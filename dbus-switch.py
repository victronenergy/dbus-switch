#!/usr/bin/python3 -u

import sys, os
import signal
from functools import partial
from argparse import ArgumentParser
import traceback
import logging
from enum import IntEnum
import threading
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

# Base class for all output types
class Pin():
	output_type = None
	name = None
	path = None
	label = None
	_status_cb = None
	_fb = None
	store_state = True
	_state = 0
	_status = STATUS_DISABLED

	def __init__(self, name=None, path=None, label=None, fb = None, status_cb=None):
		self.name = name
		self.path = path
		self.label = label
		self._fb = fb
		self._status_cb = status_cb

		if self._fb is not None:
			self.store_state = False

	@property
	def state(self):
		return self._state

	@state.setter
	def state(self, state):
		raise NotImplementedError

	@property
	def status(self):
		return self._status

	@status.setter
	def status(self, status):
		self._status = status
		if self._status_cb is not None:
			self._status_cb(self)

	@property
	def has_feedback(self):
		return self._fb is not None

	@property
	def fb_state(self):
		if self._fb:
			try:
				with open(self._fb + '/value', 'rt') as r:
					return int(r.read())
			except IOError:
				traceback.print_exc()
		raise ValueError("No feedback pin")

	@classmethod
	def createRelay(cls, id, name, paths, label, status_cb=None):
		fb = None
		set = None
		res = None
		for path in paths:
			fb = path if path.endswith('_in') else fb
			res = path if path.endswith('_res') else res
			set = path if path.endswith('_set') or path.endswith(str(id)) else set

		# Monostable relay
		if set and not res:
			return OutputPin(name, path + '/value', label, fb, status_cb)

		# Bistable relay
		if set and res:
			return BiStableRelay(name, set, res, label, fb, status_cb)

class PwmPin(Pin):
	output_type = OutputType.DIMMABLE
	_dimming = 0
	_state = 0

	@property
	def dimming(self):
		return self._dimming

	@dimming.setter
	def dimming(self, dimming):
		self._dimming = dimming
		if self._state:
			self.state = 1

	@Pin.state.setter
	def state(self, state):
		if state < 0 or state > 1:
			return
		try:
			with open(self.path, 'wt') as w:
				w.write(str(round(self._dimming * 2.55)) if state else '0')
		except IOError:
			traceback.print_exc()
			return

		self._state = state
		self.status = STATUS_ON if state else STATUS_OFF

class OutputPin(Pin):
	output_type = OutputType.TOGGLE

	@Pin.state.setter
	def state(self, state):
		if state < 0 or state > 1:
			return
		try:
			with open(self.path, 'wt') as w:
				w.write(str(round(state)))
		except IOError:
			traceback.print_exc()
			self.status = STATUS_OUTPUT_FAULT
			return

		self._state = state
		self.status = STATUS_ON if state else STATUS_OFF

class BiStableRelay(Pin):
	output_type = OutputType.TOGGLE
	PULSELEN = 2000
	CHECK_INT = 100
	retries = 0
	desired_state = 0
	def __init__(self, name=None, set=None, res=None, label=None, fb=None, status_cb=None):
		super(BiStableRelay, self).__init__(name=name, label=label, fb=fb, status_cb=status_cb)
		self.setpath = set
		self.respath = res
		self._state = self.fb_state
		self._pins_lock = threading.Lock()	# Lock for accessing the HW pins
		self._state_lock = threading.Lock()	# Lock for accessing _state
		self._timer_task = None
		self.status = STATUS_ON if self._state else STATUS_OFF
		self._clear_paths()

	@Pin.state.setter
	def state(self, state):
		with self._pins_lock:
			try:
				with open((self.setpath if state else self.respath) + '/value', 'wt') as w:
					w.write('1')
			except IOError:
				traceback.print_exc()
				return

		self._cancel_timer()
		if self.has_feedback:
			self.retries = 0
			self._timer_task = self._schedule_async(self._wait_for_state_async(state))
		else:
			self._timer_task = self._schedule_async(self._clear_after_pulse())
			self.status = STATUS_ON if self._state else STATUS_OFF

		with self._state_lock:
			self._state = state

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
			self.clear()
		except asyncio.CancelledError:
			return

	async def _wait_for_state_async(self, state):
		try:
			while True:
				if not self._waitForState(state):
					return
				await asyncio.sleep(self.CHECK_INT / 1000)
		except asyncio.CancelledError:
			return

	def _waitForState(self, state):
		with self._state_lock:
			if self._state != state:
				# State has changed while waiting.
				# Only clear the path that was set to prevent breaking the next change.
				with self._pins_lock:
					with open((self.setpath if state else self.respath) + '/value', 'wt') as w:
						w.write('0')
				return False # Stop timer
		self.retries += 1
		ret = self.fb_state != state and self.retries < self.PULSELEN / self.CHECK_INT
		if not ret:
			self._clear()
		return ret

	def _clear(self):
		if self.has_feedback:
			with self._state_lock:
				if self._state != self.fb_state:
					self.status = STATUS_OUTPUT_FAULT
				else:
					self.status = STATUS_ON if self._state else STATUS_OFF
		self._clear_paths()

	def _clear_paths(self):
		with self._pins_lock:
			for path in [self.setpath, self.respath]:
				try:
					with open(path + '/value', 'wt') as w:
						w.write('0')
				except IOError:
					traceback.print_exc()
					return False
			return True

# Base class for all switching devices.
class SwitchableOutputMixin(object):
	async def add_output(self, channel, output_type, set_state_cb, name=None, set_dimming_cb=None, store_state=False, initial_state=None):
		path_base  = '/SwitchableOutput/%s/' % channel

		# Make settings
		await self._settings.add_settings(Setting(f"/Settings/Devices/gxio_{self._serial}/{channel}/Group", "", alias=f"Group_{channel}"),
			Setting(f"/Settings/Devices/gxio_{self._serial}/{channel}/CustomName", "", alias=f"CustomName_{channel}"),
			Setting(f"/Settings/Devices/gxio_{self._serial}/{channel}/ShowUIControl", 1, 0, 6, alias=f"ShowUIControl_{channel}"),
			Setting(f"/Settings/Devices/gxio_{self._serial}/{channel}/Type", output_type, 0, 2, alias=f"Type_{channel}"))
		# No setting for function, as it can only be manual.
		if initial_state is None:	# State setting needed if no initial state provided
			await self._settings.add_settings(Setting(f"/Settings/Devices/gxio_{self._serial}/{channel}/State", 0, 0, 1, alias=f"State_{channel}"))

		if output_type == OutputType.DIMMABLE:
			await self._settings.add_settings(Setting(f"/Settings/Devices/gxio_{self._serial}/{channel}/Dimming", 0, 0, 255, alias=f"Dimming_{channel}"))

		init_state = initial_state if initial_state is not None else self._settings.get_value(self._settings.alias(f'State_{channel}'))
		self.paths.append(IntegerItem(path_base + 'State', init_state, writeable=True, onchange=set_state_cb))
		self.paths.append(IntegerItem(path_base + 'Status', STATUS_ON if init_state else STATUS_OFF, writeable=False, text=self._status_text_callback))
		self.paths.append(TextItem(path_base + 'Name', name, writeable=False))

		if output_type == OutputType.DIMMABLE:
			self.paths.append(IntegerItem(path_base + "Dimming", self._settings.get_value(self._settings.alias(f'Dimming_{channel}')), writeable=True, onchange=set_dimming_cb, text=lambda y: str(y) + '%'))

		# Settings
		validTypesDimmable = 1 << OutputType.DIMMABLE
		validTypesToggle = 1 << OutputType.TOGGLE
		validTypesMomentary = 1 << OutputType.MOMENTARY

		self.paths.append(TextItem(path_base + 'Settings/Group', "", writeable=True, onchange=partial(self._handle_changed_value, path_base + 'Settings/Group')))
		self.paths.append(TextItem(path_base + 'Settings/CustomName', "", writeable=True, onchange=partial(self._handle_changed_value, path_base + 'Settings/CustomName')))
		self.paths.append(IntegerItem(path_base + 'Settings/ShowUIControl', 1, writeable=True, onchange=partial(self._handle_changed_value, path_base + 'Settings/ShowUIControl')))
		self.paths.append(IntegerItem(path_base + 'Settings/Type', output_type, writeable=True, onchange=partial(self._handle_changed_value, path_base + 'Settings/Type'),
							text=self._type_text_callback))
		self.paths.append(IntegerItem(path_base + 'Settings/ValidTypes', validTypesDimmable if
							output_type == OutputType.DIMMABLE else validTypesToggle | validTypesMomentary,
							writeable=False, text=self._valid_types_text_callback))
		self.paths.append(IntegerItem(path_base + 'Settings/Function', OutputFunction.MANUAL, writeable=True, onchange=partial(self._handle_changed_value, path_base + 'Settings/Function'),
							text=self._function_text_callback))
		self.paths.append(IntegerItem(path_base + 'Settings/ValidFunctions', (1 << OutputFunction.MANUAL),
							writeable=False, text=self._valid_functions_text_callback))

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


class ThrottledUpdaterMixin:
	# Throttling mechanism to avoid a queue build-up on the device when the user is dragging a slider in the UI.
	# Used for dimming tasks. The dispatched task should exit quietly
	# if the value it is passed is not the desired value anymore by the time the runner wakes up.
	async def throttled_updater(self, update_task, item, value):
		async with self._throttling_lock:
			self._desired_value = value
			task = asyncio.create_task(self._throttled_updater_runner(update_task, item, value))
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

	async def _throttled_updater_runner(self, update_task, item, value):
		# Only start one updater at a time
		async with self._throttling_runner_lock:
			# If a new dimming setpoint has been set by the time this thread wakes up, then exit this one.
			async with self._throttling_lock:
				if value != self._desired_value:
					return
			await update_task(item, value)

class GxIoExtender(SwitchableOutputMixin, ThrottledUpdaterMixin):
	def __init__(self, bus_type, serial):
		self._serial = serial
		self._throttling_lock = asyncio.Lock()
		self._throttling_runner_lock = asyncio.Lock()
		self._write_dimming_timer = None
		self._write_dimming_task = None
		self.bus_type = bus_type
		self._productId = PRODUCT_ID
		self._interface = "USB"
		self._productNameSettings = 'gxio_%s' % self._serial
		self._serviceName = 'com.victronenergy.switch.%s' % self._serial
		self._productName = 'GX IO extender 150'
		self._dbusService = None
		self._settings = None
		self.paths = []

	async def ainit(self):
		config_file = "/run/io-ext/{}/pins.conf".format(self._serial)
		if not os.path.exists(config_file):
			raise FileNotFoundError("Configuration file not found: {}".format(config_file))

		self.pins = self.parse_config(config_file)

		self.bus = await MessageBus(bus_type=self.bus_type).connect()
		self._settings = await wait_for_settings(self.bus, itemsChanged=self._handle_changed_setting)

		# Obtain the class and vrm instance from localsettings
		await self._settings.add_settings(Setting(f"/Settings/Devices/{self._productNameSettings}/ClassAndVrmInstance", "switch:50", alias="deviceinstance"),
			Setting(f"/Settings/Devices/{self._productNameSettings}/CustomName", "", alias="customname"))

		# Generic module paths
		self.paths.append(TextItem('/Mgmt/ProcessName', sys.argv[0]))
		self.paths.append(TextItem('/Mgmt/ProcessVersion', VERSION))
		self.paths.append(TextItem('/Mgmt/Connection', self._interface))
		self.paths.append(IntegerItem('/ProductId', self._productId, text=fmt['productid']))
		self.paths.append(TextItem('/ProductName', self._productName))
		self.paths.append(IntegerItem('/Connected', 1))
		self.paths.append(TextItem('/Serial', self._serial))
		self.paths.append(TextItem('/CustomName', self._settings.get_value(self._settings.alias('customname')), writeable=True, onchange=partial(self._handle_changed_value, '/CustomName')))
		self.paths.append(IntegerItem('/State', ModuleState.CONNECTED, writeable=False, text=self._module_state_text_callback))
		self.paths.append(IntegerItem('/DeviceInstance', int(self._settings.get_value(self._settings.alias('deviceinstance')).split(':')[-1])))

		# Add outputs
		await asyncio.gather(*(self.add_output(pin.name, pin.output_type,
				   partial(self.set_hw_state, pin, 'State_%s' % pin.name),
				   name=pin.label,
				   set_dimming_cb=
				   partial(self.throttled_updater, partial(self.set_dimming, pin)) if pin.output_type == OutputType.DIMMABLE else None,
				   initial_state=None if pin.store_state else pin.state) for pin in self.pins))

		# Set initial pin state
		for pin in self.pins:
			if pin.store_state:
				if pin.output_type == OutputType.DIMMABLE:
					pin.dimming = self._settings.get_value(self._settings.alias(f'Dimming_{pin.name}'))
				pin.state = self._settings.get_value(self._settings.alias(f'State_{pin.name}'))
			else:
				pin.state = pin.state # Trigger status update

		# Create the D-Bus service
		self._dbusService = Service(self.bus, self._serviceName)

		# Add all paths to the service
		for path in self.paths:
			self._dbusService.add_item(path)

		# Register on dbus
		await self._dbusService.register()

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
		if self._write_dimming_timer:
			self._write_dimming_timer.cancel()
		if self._write_dimming_task or self._write_dimming_timer:
			await self._write_dimming()
		self._write_dimming_timer = None
		self._write_dimming_task = None

		if self._dbusService:
			await self._dbusService.close()

		if self.bus:
			self.bus.disconnect()

	def _handle_changed_setting(self, service, values):
		for p, v in values.items():
			setting = p.split('/')[-1]
			newvalue = v
			if setting == 'CustomName':
				with self._dbusService as s:
					s['/CustomName'] = newvalue

	def status_cb(self, pin):
		if self._dbusService:
			with self._dbusService as s:
				s['/SwitchableOutput/%s/Status' % pin.name] = pin.status

	async def set_hw_state(self, pin, setting, item, value):
		if value < 0 or value > 1:
			return
		pin.state = value
		if pin.store_state:
			await self._settings.set_value(self._settings.alias(setting), pin.state)
		item.set_local_value(value)

	async def set_dimming(self, pin, item, value):
		if value < 0 or value > 100:
			return False
		pin.dimming = value
		# Throttle writing the dimming state to localsettings.
		if self._write_dimming_timer:
			self._write_dimming_timer.cancel()
			self._write_dimming_timer = None
		loop = asyncio.get_running_loop()

		self._write_dimming_timer = loop.call_later(1, self._start_write_dimming)
		item.set_local_value(value)

	def _start_write_dimming(self):
			self._write_dimming_task = asyncio.create_task(self._write_dimming())
			self._write_dimming_task.add_done_callback(self._clear_write_dimming_task)

	def _clear_write_dimming_task(self, task):
		if self._write_dimming_task is task:
			self._write_dimming_task = None

	async def _write_dimming(self):
		for pin in [p for p in self.pins if p.output_type == OutputType.DIMMABLE]:
			if pin.dimming != self._settings.get_value(self._settings.alias(f'Dimming_{pin.name}')):
				await self._settings.set_value(
					self._settings.alias(f'Dimming_{pin.name}'), pin.dimming)
		self._write_dimming_timer = None

	async def _handle_changed_value(self, path, item, value):
		if path.endswith('/CustomName'):
			if await self._settings.set_value(self._settings.alias('customname'), value) < 0:
				return

		else:
			if path.endswith('/Type'):
				validTypesPath = path.replace('/Type', '/ValidTypes')
				if not ((1 << value) & self._dbusService[validTypesPath]):
					return

				if int(value) == OutputType.MOMENTARY:
					# Reset the state when the momentary type is selected
					state_item = self._dbusService.get_item(path.replace('/Settings/Type', '/State'))
					if state_item:
						state_item._set_value(0)

			elif path.endswith('/Function'):
				validFunctionsPath = path.replace('/Function', '/ValidFunctions')
				if not ((1 << value) & self._dbusService[validFunctionsPath]):
					return

			split = path.split('/')
			if len(split) > 3 and split[3] == 'Settings':
				setting = split[-1] + '_' + split[-3]
				try:
					if await self._settings.set_value(self._settings.alias(setting), value) < 0:
						return
				except KeyError:
					return
			else:
				return

		item.set_local_value(value)

	def parse_config(self, conf):
		f = open(conf)

		pins = []
		for line in f:
			cmd, arg = line.strip().split(maxsplit=1)

			if cmd == 'relay' or cmd == 'ssr':
				pth, id = arg.split(maxsplit=1)
				name = "relay_" + id
				label = ("Relay " if cmd == 'relay' else "Solid state relay ") + id

				pths = []
				for x in os.listdir(os.path.dirname(pth)):
					if x.startswith(os.path.basename(pth)):
						pths.append(os.path.join(os.path.dirname(pth), x))
				pin = Pin.createRelay(id, name, pths, label, status_cb=self.status_cb)
				pins.append(pin)
				continue

			if cmd == 'pwm':
				pth, id = arg.split(maxsplit=1)
				name = "pwm_" + id
				label = "PWM " + id
				pin = PwmPin(name, pth, label, status_cb=self.status_cb)
				pins.append(pin)
				continue

			if cmd == 'output':
				pth, id = arg.split(maxsplit=1)
				name = "output_" + id
				label = "Output " + id
				pth = pth + '/value'
				pin = OutputPin(name, pth, label, status_cb=self.status_cb)
				pins.append(pin)
				continue

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
