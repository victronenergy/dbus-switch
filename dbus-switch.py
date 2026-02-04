#!/usr/bin/python3 -u

import sys, os
import signal
from functools import partial
from select import epoll, EPOLLIN, EPOLLPRI, EPOLLERR
from argparse import ArgumentParser
import traceback
import logging
from enum import IntEnum
import asyncio

try:
	from dbus_fast.aio import MessageBus
except ImportError:
	from dbus_next.aio import MessageBus

# 3rd party
try:
	from dbus_fast.constants import BusType
except ImportError:
	from dbus_next.constants import BusType

sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.service import Service, IntegerItem, TextItem
from aiovelib.localsettings import Setting

from utils import wait_for_settings, formatters as fmt, logger

VERSION = '0.11'
PRODUCT_ID = 0xC01A

class OutputType(IntEnum):
	MOMENTARY = 0
	TOGGLE = 1
	DIMMABLE = 2
	RGB = 11
	TYPE_MAX = RGBW = 13

class OutputFunction(IntEnum):
	ALARM = 0
	GENSET_START_STOP = 1
	MANUAL = 2
	TANK_PUMP = 3
	TEMPERATURE = 4
	CONNECTED_GENSET_HELPER_RELAY = 5
	S2_RM = 6

class InputType(IntEnum):
	DISCRETE_VALUE_INDICATOR = 0
	# For the IO extender, we only need the discrete type.

class ModuleState(IntEnum):
	CONNECTED = 0x100
	OVER_TEMPERATURE = 0x101
	TEMPERATURE_WARNING = 0x102
	CHANNEL_FAULT = 0x103
	CHANNEL_TRIPPED = 0x104
	UNDER_VOLTAGE = 0x105

STATUS_OFF = 0x00
STATUS_ON = 0x09
STATUS_OUTPUT_FAULT = 0x08
STATUS_DISABLED = 0x20

background_tasks = set()

class PinIo:
	def __init__(self, interval=1.0):
		self._interval = interval
		self._pins = {}
		self._fds = {}
		self._task = None
		self._running = False
		self.ob = epoll()

	def register(self, pin):
		if pin in self._pins:
			return
		path = getattr(pin, "path", None)
		if not path:
			return
		# Set up gpio for rising edge interrupts
		try:
			with open(os.path.join(os.path.dirname(path), 'edge'), 'ab') as fd:
				fd.write(b'both')
		except:
			pass

		fd = os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), 'rb')
		self._pins[pin] = None
		self._fds[pin] = fd
		self.ob.register(fd.fileno(), EPOLLPRI)

	def unregister(self, pin):
		fd = self._fds.pop(pin, None)
		self._pins.pop(pin, None)
		if fd is None:
			return
		try:
			self.ob.unregister(fd.fileno())
		except OSError:
			pass
		try:
			fd.close()
		except OSError:
			pass

	def start(self):
		if self._running:
			return
		self._running = True
		if self._task is None:
			self._task = asyncio.get_running_loop().create_task(self._run())

	async def stop(self):
		self._running = False
		if self._task and not self._task.done():
			self._task.cancel()
			try:
				await self._task
			except asyncio.CancelledError:
				pass
		self._task = None
		for fd in list(self._fds.values()):
			try:
				self.ob.unregister(fd.fileno())
			except OSError:
				pass
			try:
				fd.close()
			except OSError:
				pass
		self._fds.clear()
		self._pins.clear()

	async def _run(self):
		try:
			while self._running:
				await asyncio.get_running_loop().run_in_executor(
					None, self.ob.poll, self._interval
				)

				for pin, fd in list(self._fds.items()):
					try:
						os.lseek(fd.fileno(), 0, os.SEEK_SET)
						v = int(fd.read(1))

						if v != self._pins.get(pin):
							pin.set_value(v)
							self._pins[pin] = v
					except:
						continue
		except asyncio.CancelledError:
			return

PIN_IO = PinIo()

# Base class for all pins.
class Pin():
	@classmethod
	def create(cls, service, settings, serial, cmd, arg):
		pin = None
		pth, id = arg.split(maxsplit=1)
		if cmd == 'relay' or cmd == 'ssr':
			name = "relay_" + id
			label = ("Relay " if cmd == 'relay' else "Solid state relay ") + id

			paths = []
			for x in os.listdir(os.path.dirname(pth)):
				if x.startswith(os.path.basename(pth)):
					paths.append(os.path.join(os.path.dirname(pth), x))

			fb_path = set_path = res_path = None
			for path in paths:
				fb_path = path if path.endswith('_in') else fb_path
				res_path = path if path.endswith('_res') else res_path
				set_path = path if path.endswith('_set') or path.endswith(str(id)) else set_path

			if set_path and not res_path: # Monostable relay
				pin = OutputPin(name, pth + '/value', label)
			elif set_path and res_path: # Bistable relay
				pin = BiStableRelay(name, set_path + '/value', res_path + '/value', label, fb_path + '/value' if fb_path else None)
			else:
				raise ValueError("Invalid relay configuration for pin id {}".format(id))

		elif cmd == 'pwm':
			name = "pwm_" + id
			label = "PWM Output " + id
			pin = PwmPin(name, pth, label)

		elif cmd == 'output':
			name = "output_" + id
			label = "Output " + id
			pin = OutputPin(name, pth + '/value', label)

		elif cmd == 'input':
			name = "input_" + id
			label = "Input " + id
			pin = InputPin(name, pth + '/value', label)

		else:
			raise ValueError("Unknown pin type: {}".format(cmd))

		pin.set_resources(service, settings, serial)
		return pin

	def __init__(self, name=None, path=None, label=None):
		self._type = None
		self._valid_types = None
		self._function = OutputFunction.MANUAL
		self._valid_functions = (1 << OutputFunction.MANUAL)
		self._store_state = True
		self._state = 0
		self._status = STATUS_DISABLED
		self.name = name
		self.path = path
		self.label = label

	def set_resources(self, service, settings, serial):
		self._service = service
		self._settings = settings
		self._serial = serial

	def types_to_mask(self, types):
		mask = 0
		for t in types:
			mask |= (1 << t)
		return mask

	async def ainit(self):
		if self._service is None or self._settings is None:
			raise RuntimeError("Pin resources not set")
		channel = self.name
		store_type = False
		# Make settings
		settings = [Setting(f"/Settings/Devices/gxio_{self._serial}/{channel}/Group", "", alias=f"Group_{channel}"),
			Setting(f"/Settings/Devices/gxio_{self._serial}/{channel}/CustomName", "", alias=f"CustomName_{channel}"),
			Setting(f"/Settings/Devices/gxio_{self._serial}/{channel}/ShowUIControl", 1, 0, 6, alias=f"ShowUIControl_{channel}")]
		# Only store the type if there are multiple valid types
		if int.bit_count(self._valid_types) > 1:
			store_type = True
			settings.append(Setting(f"/Settings/Devices/gxio_{self._serial}/{channel}/Type", self._type, 0, 2, alias=f"Type_{channel}"))

		await self._settings.add_settings(*settings)
		if store_type:
			self._type = self._settings.get_value(self._settings.alias(f'Type_{channel}'))

		# Generic channel paths
		self._service.add_item(TextItem(self._path_base + 'Settings/Group', self._settings.get_value(self._settings.alias(f'Group_{channel}')), writeable=True, onchange=partial(self._handle_changed_value, self._path_base + 'Settings/Group')))
		self._service.add_item(TextItem(self._path_base + 'Settings/CustomName', self._settings.get_value(self._settings.alias(f'CustomName_{channel}')), writeable=True, onchange=partial(self._handle_changed_value, self._path_base + 'Settings/CustomName')))
		self._service.add_item(IntegerItem(self._path_base + 'Settings/ShowUIControl', self._settings.get_value(self._settings.alias(f'ShowUIControl_{channel}')), writeable=True, onchange=partial(self._handle_changed_value, self._path_base + 'Settings/ShowUIControl')))
		self._service.add_item(TextItem(self._path_base + 'Name', self.label, writeable=False))

		self._service.add_item(IntegerItem(self._path_base + 'Settings/Type', self._type, writeable=True, onchange=partial(self._handle_changed_value, self._path_base + 'Settings/Type'),
							text=self._type_text_callback))
		self._service.add_item(IntegerItem(self._path_base + 'Settings/ValidTypes', self._valid_types, writeable=False, text=self._valid_types_text_callback))
		self._service.add_item(IntegerItem(self._path_base + 'Settings/Function', OutputFunction.MANUAL, writeable=True, onchange=partial(self._handle_changed_value, self._path_base + 'Settings/Function'),
							text=self._function_text_callback))
		self._service.add_item(IntegerItem(self._path_base + 'Settings/ValidFunctions', (1 << OutputFunction.MANUAL),
							writeable=False, text=self._valid_functions_text_callback))

	async def stop(self):
		pass

	def write(self, path, value):
		try:
			with open(path, "wt") as w:
				w.write(str(value))
			return True
		except IOError:
			traceback.print_exc()
			return False

	def read(self, path):
		try:
			with open(path, "rt") as r:
				return int(r.read())
		except IOError:
			traceback.print_exc()
			return None

	def supports_interrupts(self):
		return False

	async def _handle_changed_value(self, path, item, value):
		split = path.split('/')
		if len(split) > 3 and split[3] == 'Settings':
			if path.endswith('/Type'):
				validTypesPath = path.replace('/Type', '/ValidTypes')
				if not ((1 << value) & self._service[validTypesPath]):
					return

				self.output_type = OutputType(value)
				if int(value) == OutputType.MOMENTARY:
					if self._store_state:
						await self._settings.set_value(self._settings.alias(f'State_{split[-3]}'), 0)
					# Reset the state when the momentary type is selected
					state_item = self._service.get_item(path.replace('/Settings/Type', '/State'))
					if state_item:
						state_item._set_value(0)

			elif path.endswith('/Function'):
				validFunctionsPath = path.replace('/Function', '/ValidFunctions')
				if not ((1 << value) & self._service[validFunctionsPath]):
					return

			# Other setting specific for input or output, pass on
			else:
				self._on_setting_changed(split[-1], value)

			setting = split[-1] + '_' + split[-3]
			try:
				if await self._settings.set_value(self._settings.alias(setting), value) < 0:
					return
			except KeyError:
				return
		else:
			return

		item.set_local_value(value)

	def _on_setting_changed(self, path, value):
		pass

	async def set_hw_state(self, state):
		raise NotImplementedError

	# Formatters
	def _status_text_callback(self, value):
		status_map = {
			0x00: "Off",
			0x09: "On",
			0x02: "Tripped",
			0x04: "Over temperature",
			0x01: "Powered",
			0x08: "Output fault",
			0x10: "Short fault",
			0x20: "Disabled",
		}
		return status_map.get(value, "Unknown")

	def _type_text_callback(self, value):
		type_map = {
			OutputType.MOMENTARY: "Momentary",
			OutputType.TOGGLE: "Toggle",
			OutputType.DIMMABLE: "Dimmable",
			OutputType.RGB: "RGB",
			OutputType.RGBW: "RGBW",
		}
		return type_map.get(value, "Unknown")

	def _function_text_callback(self, value):
		function_map = {
			OutputFunction.ALARM: "Alarm",
			OutputFunction.GENSET_START_STOP: "Genset start stop",
			OutputFunction.MANUAL: "Manual",
			OutputFunction.TANK_PUMP: "Tank pump",
			OutputFunction.TEMPERATURE: "Temperature",
			OutputFunction.CONNECTED_GENSET_HELPER_RELAY: "Connected genset helper relay",
			OutputFunction.S2_RM: "S2 resource manager",
		}
		return function_map.get(value, "Unknown")

	def _bitmask_text(self, value, entries):
		parts = []
		for flag, label in entries:
			if value & (1 << flag):
				parts.append(label)
		return ", ".join(parts)

	def _valid_functions_text_callback(self, value):
		entries = [
			(OutputFunction.ALARM, "Alarm"),
			(OutputFunction.GENSET_START_STOP, "Genset start stop"),
			(OutputFunction.MANUAL, "Manual"),
			(OutputFunction.TANK_PUMP, "Tank pump"),
			(OutputFunction.TEMPERATURE, "Temperature"),
			(OutputFunction.CONNECTED_GENSET_HELPER_RELAY, "Connected genset helper relay"),
			(OutputFunction.S2_RM, "S2 resource manager"),
		]
		return self._bitmask_text(value, entries)

	def _valid_types_text_callback(self, value):
		entries = [
			(OutputType.DIMMABLE, "Dimmable"),
			(OutputType.TOGGLE, "Toggle"),
			(OutputType.MOMENTARY, "Momentary"),
			(OutputType.RGB, "RGB"),
			(OutputType.RGBW, "RGBW"),
		]
		return self._bitmask_text(value, entries)

class InputPin(Pin):
	def __init__(self, name=None, path=None, label=None):
		super(InputPin, self).__init__(name=name, path=path, label=label)
		self._path_base = f'/GenericInput/{name}/'
		self._type = InputType.DISCRETE_VALUE_INDICATOR
		self._valid_types = self.types_to_mask([InputType.DISCRETE_VALUE_INDICATOR])
		self._invert = False
		self._value_item = None

	async def ainit(self):
		await super().ainit()

		await self._settings.add_settings(Setting(f"/Settings/Devices/gxio_{self._serial}/{self.name}/Invert", 0, 0, 1, alias=f"Invert_{self.name}"))

		self._service.add_item(IntegerItem(self._path_base + 'Status', 0x00, writeable=False)) # status 0x00: OK
		self._value_item = IntegerItem(self._path_base + 'Value', self.read(self.path), writeable=False)
		self._service.add_item(self._value_item)
		self._service.add_item(IntegerItem(self._path_base + 'Settings/Invert', 0, writeable=True, onchange=partial(self._handle_changed_value, self._path_base + 'Settings/Invert')))
		self._service.add_item(TextItem(self._path_base + 'Settings/Unit', "", writeable=True, onchange=self.set_unit))

		PIN_IO.register(self)

	async def set_unit(self, item, value):
		item.set_local_value(value)

	def _on_setting_changed(self, setting, value):
		if setting == 'Invert':
			self._invert = bool(value)
			self.set_value(self.read(self.path))

	def set_value(self, value):
		if self._value_item:
			if self._invert:
				value = 0 if value else 1
			self._value_item.set_local_value(value)

	async def stop(self):
		PIN_IO.unregister(self)

class OutputPin(Pin):
	def __init__(self, name=None, path=None, label=None):
		super(OutputPin, self).__init__(name=name, path=path, label=label)
		self._path_base = f'/SwitchableOutput/{name}/'
		self._type = OutputType.TOGGLE
		self._valid_types = self.types_to_mask([OutputType.MOMENTARY, OutputType.TOGGLE])

	async def ainit(self):
		await super().ainit()
		# Fetch type from settings
		self._type = OutputType(self._settings.get_value(self._settings.alias(f'Type_{self.name}')))

		if self._store_state:
			await self._settings.add_settings(Setting(f"/Settings/Devices/gxio_{self._serial}/{self.name}/State", 0, 0, 1, alias=f"State_{self.name}"))
			self._state = self._settings.get_value(self._settings.alias(f'State_{self.name}'))

		self._service.add_item(IntegerItem(self._path_base + 'State', self._state, writeable=True, onchange=self.set_state))
		self._service.add_item(IntegerItem(self._path_base + 'Status', STATUS_ON if self._state else STATUS_OFF, writeable=False, text=self._status_text_callback))
		# Set initial state
		if self._store_state:
			await self.set_hw_state(self._settings.get_value(self._settings.alias(f'State_{self.name}')))

	def set_status(self, status):
		self._status = status
		if self._service:
			with self._service as s:
				s[f'{self._path_base}Status'] = self._status

	async def set_state(self, item, value):
		if value < 0 or value > 1:
			return
		await self.set_hw_state(value)
		item.set_local_value(value)
		# No point in storing the state for momentary outputs as they should always start off.
		if self._store_state and self._type != OutputType.MOMENTARY:
			await self._settings.set_value(self._settings.alias(f'State_{self.name}'), self._state)

	async def set_hw_state(self, state):
		if state < 0 or state > 1:
			return
		if not self.write(self.path, str(round(state))):
			return

		self._state = state
		self.set_status(STATUS_ON if state else STATUS_OFF)

class PwmPin(OutputPin):
	def __init__(self, name=None, path=None, label=None):
		super(PwmPin, self).__init__(name=name, path=path, label=label)
		self._type = OutputType.DIMMABLE
		self._valid_types = self.types_to_mask([OutputType.DIMMABLE])
		self._dimming = 0
		self._write_dimming_timer = None
		self._write_dimming_task = None

	async def ainit(self):
		await super().ainit()

		await self._settings.add_settings(Setting(f"/Settings/Devices/gxio_{self._serial}/{self.name}/Dimming", 0, 0, 100, alias=f"Dimming_{self.name}"))
		init_dimming = self._settings.get_value(self._settings.alias(f'Dimming_{self.name}'))
		self._service.add_item(IntegerItem(self._path_base + "Dimming", init_dimming, writeable=True, onchange=self.set_dimming, text=lambda y: str(y) + '%'))

		# Set initial state
		item = self._service.get_item(self._path_base + "Dimming")
		if item:
			self.set_dimming(item, init_dimming)

	async def stop(self):
		if self._write_dimming_timer:
			self._write_dimming_timer.cancel()
		if self._write_dimming_task or self._write_dimming_timer:
			await self._write_dimming()
		self._write_dimming_timer = None
		self._write_dimming_task = None

	def set_dimming(self, value):
		if value < 0 or value > 100:
			return False
		self._dimming = value
		if self._state:
			self.set_dimming_state(1)
		# Throttle writing the dimming state to localsettings.
		if self._write_dimming_timer:
			self._write_dimming_timer.cancel()
			self._write_dimming_timer = None
		loop = asyncio.get_running_loop()

		self._write_dimming_timer = loop.call_later(1, self._start_write_dimming)
		return True

	def _start_write_dimming(self):
			self._write_dimming_task = asyncio.create_task(self._write_dimming())
			self._write_dimming_task.add_done_callback(self._clear_write_dimming_task)

	def _clear_write_dimming_task(self, task):
		if self._write_dimming_task is task:
			self._write_dimming_task = None

	async def _write_dimming(self):
		if self._dimming != self._settings.get_value(self._settings.alias(f'Dimming_{self.name}')):
			await self._settings.set_value(self._settings.alias(f'Dimming_{self.name}'), self._dimming)
		self._write_dimming_timer = None

	async def set_state(self, item, value):
		if value < 0 or value > 1:
			return
		self.set_dimming_state(value)
		item.set_local_value(value)

	def set_dimming_state(self, state):
		if state < 0 or state > 1:
			return
		if not self.write(self.path, str(round(self._dimming * 2.55)) if state else '0'):
			return

		self._state = state
		self.set_status(STATUS_ON if state else STATUS_OFF)


class BiStableRelay(OutputPin):
	PULSELEN = 1000 # milliseconds
	CHECK_INT = 100 # milliseconds
	def __init__(self, name=None, set_path=None, res_path=None, label=None, fb_path=None):
		super(BiStableRelay, self).__init__(name=name, label=label)
		self._type = OutputType.TOGGLE
		self._valid_types = self.types_to_mask([OutputType.MOMENTARY, OutputType.TOGGLE])
		self._setpath = set_path
		self._respath = res_path
		self._fb = fb_path
		self._retries = 0
		self._state_lock = asyncio.Lock()
		self._timer_task = None

		if self._fb is not None:
			self._store_state = False

	async def ainit(self):
		if self._fb is not None:
			self._state = self.fb_state_async()
		await super().ainit()
		self.set_status(STATUS_ON if self._state else STATUS_OFF)
		self._clear_paths()

	async def set_hw_state(self, state):
		if not self.write(self._setpath if state else self._respath, '1'):
			return

		self._cancel_timer()
		if self._fb is not None:
			self._retries = 0
			self._timer_task = self._schedule_async(self._wait_for_state_async(state))
		else:
			self._timer_task = self._schedule_async(self._clear_after_pulse())
			self.set_status(STATUS_ON if self._state else STATUS_OFF)

		async with self._state_lock:
			self._state = state

	def fb_state_async(self):
		if self._fb:
			return self.read(self._fb) or 0
		return self._state

	def _schedule_async(self, coro):
		# Assumes this is called on the running asyncio loop thread.
		return asyncio.get_running_loop().create_task(coro)

	def _cancel_timer(self):
		if self._timer_task and not self._timer_task.done():
			self._timer_task.cancel()
		self._timer_task = None

	async def _clear_after_pulse(self):
		try:
			await asyncio.sleep(self.PULSELEN / 1000)
			await self._clear_async(self._state)
		except asyncio.CancelledError:
			return

	async def _wait_for_state_async(self, state):
		try:
			while True:
				if not await self._wait_for_state(state):
					return
				await asyncio.sleep(self.CHECK_INT / 1000)
		except asyncio.CancelledError:
			# Clear the path that was set to prevent breaking the next change.
			self.write(self._setpath if state else self._respath, '0')
			return

	async def _wait_for_state(self, state):
		async with self._state_lock:
			if self._state != state:
				# State has changed while waiting.
				# Only clear the path that was set to prevent breaking the next change.
				self.write(self._setpath if state else self._respath, '0')
				return False # Stop timer
		self._retries += 1
		fb_state = self.fb_state_async()
		if fb_state == state or self._retries >= self.PULSELEN / self.CHECK_INT:
			await self._clear_async(fb_state)
			return False # Stop timer
		return True # Continue timer

	async def _clear_async(self, fb_state):
		if self._fb is not None:
			async with self._state_lock:
				if self._state != fb_state:
					self.set_status(STATUS_OUTPUT_FAULT)
				else:
					self.set_status(STATUS_ON if self._state else STATUS_OFF)
		self._clear_paths()

	def _clear_paths(self):
		self.write(self._setpath, '0')
		self.write(self._respath, '0')

class GxIoExtender():
	def __init__(self, bus_type, serial):
		self._serial = serial
		self.bus_type = bus_type
		self._productId = PRODUCT_ID
		self._interface = "USB"
		self._productNameSettings = 'gxio_%s' % self._serial
		self._serviceName = 'com.victronenergy.switch.%s' % self._serial
		self._productName = 'GX IO extender 150'
		self._dbusService = None
		self._settings = None

	async def ainit(self):
		config_file = "/run/io-ext/{}/pins.conf".format(self._serial)
		if not os.path.exists(config_file):
			raise FileNotFoundError("Configuration file not found: {}".format(config_file))

		self.bus = await MessageBus(bus_type=self.bus_type).connect()
		self._settings = await wait_for_settings(self.bus, itemsChanged=self._handle_changed_setting)

		# Create the D-Bus service
		self._dbusService = Service(self.bus, self._serviceName)

		self.pins = self.parse_config(config_file)
		if self.pins is None or len(self.pins) == 0:
			raise RuntimeError("No pins configured")

		# Obtain the class and vrm instance from localsettings
		await self._settings.add_settings(Setting(f"/Settings/Devices/{self._productNameSettings}/ClassAndVrmInstance", "switch:50", alias="deviceinstance"),
			Setting(f"/Settings/Devices/{self._productNameSettings}/CustomName", "", alias="customname"))

		# Generic module paths
		self._dbusService.add_item(TextItem('/Mgmt/ProcessName', sys.argv[0]))
		self._dbusService.add_item(TextItem('/Mgmt/ProcessVersion', VERSION))
		self._dbusService.add_item(TextItem('/Mgmt/Connection', self._interface))
		self._dbusService.add_item(IntegerItem('/ProductId', self._productId, text=fmt['productid']))
		self._dbusService.add_item(TextItem('/ProductName', self._productName))
		self._dbusService.add_item(IntegerItem('/Connected', 1))
		self._dbusService.add_item(TextItem('/Serial', self._serial))
		self._dbusService.add_item(TextItem('/CustomName', self._settings.get_value(self._settings.alias('customname')), writeable=True, onchange=partial(self._set_customname)))
		self._dbusService.add_item(IntegerItem('/State', ModuleState.CONNECTED, writeable=False, text=self._module_state_text_callback))
		self._dbusService.add_item(IntegerItem('/DeviceInstance', int(self._settings.get_value(self._settings.alias('deviceinstance')).split(':')[-1])))

		try:
			await asyncio.gather(*(pin.ainit() for pin in self.pins), return_exceptions=True)
		except Exception as e:
			logger.error("Error initializing pins: {}".format(e))

		# Register on dbus
		await self._dbusService.register()
		PIN_IO.start()

	def _module_state_text_callback(self, value):
		state_map = {
			ModuleState.CONNECTED: "Connected",
			ModuleState.OVER_TEMPERATURE: "Over temperature",
			ModuleState.TEMPERATURE_WARNING: "Temperature warning",
			ModuleState.CHANNEL_FAULT: "Channel fault",
			ModuleState.CHANNEL_TRIPPED: "Channel tripped",
			ModuleState.UNDER_VOLTAGE: "Under voltage",
		}
		return state_map.get(value, "Unknown")

	async def stop(self):
		try:
			await asyncio.gather(*(pin.stop() for pin in self.pins), return_exceptions=True)
		except Exception as e:
			logger.error("Error stopping pins: {}".format(e))

		await PIN_IO.stop()
		if self._dbusService:
			await self._dbusService.close()

		if self.bus:
			self.bus.disconnect()
		return

	def _handle_changed_setting(self, service, values):
		for p, v in values.items():
			if p == '/CustomName':
				with self._dbusService as s:
					s['/CustomName'] = v

	async def _set_customname(self, item, value):
		await self._settings.set_value(self._settings.alias('customname'), value)
		item.set_local_value(value)

	def parse_config(self, conf):
		f = open(conf)

		pins = []
		for line in f:
			try:
				cmd, arg = line.strip().split(maxsplit=1)
				if cmd == 'tag':
					continue
				pins.append(Pin.create(self._dbusService, self._settings, self._serial, cmd, arg))
			except Exception as e:
				logger.error("Error parsing line in config file '{}': {}".format(line.strip(), e))
		f.close()
		return pins

if __name__ == '__main__':
	parser = ArgumentParser(description=sys.argv[0])
	parser.add_argument('-s', '--serial', default=None, help='serial number')
	parser.add_argument('--dbus', help='dbus bus to use, defaults to system',
			default='system')
	parser.add_argument('-d', '--debug', help='set logging level to debug',
						action='store_true')
	args = parser.parse_args()

	bus_type = {
		"system": BusType.SYSTEM,
		"session": BusType.SESSION
	}.get(args.dbus, BusType.SESSION)

	print('-------- dbus-switch, v' + VERSION + ' is starting up --------')

	logging.basicConfig(format='%(levelname)-8s %(message)s',
		level=(logging.DEBUG if args.debug else logging.INFO))

	mainloop = asyncio.new_event_loop()

	def stop_mainloop():
		mainloop.stop()

	mainloop.add_signal_handler(signal.SIGTERM, stop_mainloop)
	mainloop.add_signal_handler(signal.SIGINT, stop_mainloop)

	asyncio.set_event_loop(mainloop)
	ioExtender = GxIoExtender(bus_type, args.serial)

	try:
		mainloop.run_until_complete(ioExtender.ainit())
		logger.info("Starting main loop")
		mainloop.run_forever()
	finally:
		mainloop.run_until_complete(ioExtender.stop())
		mainloop.stop()
		mainloop.close()

	sys.exit()
