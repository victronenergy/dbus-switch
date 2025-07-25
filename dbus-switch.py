#!/usr/bin/python3 -u

import sys, os
import signal
from functools import partial
from collections import namedtuple
from argparse import ArgumentParser
import traceback
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))

from dbus.mainloop.glib import DBusGMainLoop
import dbus
from gi.repository import GLib
from logger import setup_logging
from vedbus import VeDbusService, VeDbusItemImport
from settingsdevice import SettingsDevice

VERSION = '0.9'
PRODUCT_ID = 0xC01A

OUTPUT_TYPE_MOMENTARY = 0
OUTPUT_TYPE_TOGGLE = 1
OUTPUT_TYPE_DIMMABLE = 2

OUTPUT_FUNCTION_ALARM = 0
OUTPUT_FUNCTION_GENSET_START_STOP = 1
OUTPUT_FUNCTION_MANUAL = 2
OUTPUT_FUNCTION_TANK_PUMP = 3
OUTPUT_FUNCTION_TEMPERATURE = 4
OUTPUT_FUNCTION_CONNECTED_GENSET_HELPER_RELAY = 5

MODULE_STATE_CONNECTED = 0x100
MODULE_STATE_OVER_TEMPERATURE = 0x101
MODULE_STATE_TEMPERATURE_WARNING = 0x102
MODULE_STATE_CHANNEL_FAULT = 0x103
MODULE_STATE_CHANNEL_TRIPPED = 0x104
MODULE_STATE_UNDER_VOLTAGE = 0x105

STATUS_OFF = 0x00
STATUS_ON = 0x09
STATUS_OUTPUT_FAULT = 0x08
STATUS_DISABLED = 0x20

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
	output_type = OUTPUT_TYPE_DIMMABLE
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
	output_type = OUTPUT_TYPE_TOGGLE

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
	output_type = OUTPUT_TYPE_TOGGLE
	PULSELEN = 2000
	CHECK_INT = 100
	retries = 0
	desired_state = 0
	def __init__(self, name=None, set=None, res=None, label=None, fb=None, status_cb=None):
		super(BiStableRelay, self).__init__(name=name, label=label, fb=fb, status_cb=status_cb)
		self.setpath = set
		self.respath = res
		self._state = self.fb_state
		self.status = STATUS_ON if self._state else STATUS_OFF
		self._clear_paths()

	@Pin.state.setter
	def state(self, state):
		try:
			with open((self.setpath if state else self.respath) + '/value', 'wt') as w:
				w.write('1')
		except IOError:
			traceback.print_exc()
			return

		if self._fb:
			self.retries = 0
			self.timer = GLib.timeout_add(self.CHECK_INT, self._waitForState, state)
		else:
			print("No fb pin")
			self.timer = GLib.timeout_add(self.PULSELEN, self.clear)
			self.status = STATUS_ON if self._state else STATUS_OFF

		self._state = state

	def _waitForState(self, state):
		self.retries += 1
		ret = self.state != state and self.retries < self.PULSELEN / self.CHECK_INT
		if not ret:
			self._clear()
		return ret

	def _clear(self):
		if self.has_feedback:
			if self._state != self.fb_state:
				self.status = STATUS_OUTPUT_FAULT
			else:
				self.status = STATUS_ON if self._state else STATUS_OFF

		self._clear_paths()

	def _clear_paths(self):
		for path in [self.setpath, self.respath]:
			try:
				with open(path + '/value', 'wt') as w:
					w.write('0')
			except IOError:
				traceback.print_exc()
				return False
		return True

# Base class for all switching devices.
class SwitchingDevice(object):

	_productName = 'Switching device'
	paths = {}
	settings = {}
	_dbusService = None
	_settings = None

	def __init__(self, product_id, tty="", interface="", serial=""):
		self._productId = product_id
		self._interface = interface
		self._serial = serial
		self._tty = tty
		self._productNameSettings = 'gxio_%s' % self._tty
		self._serviceName = 'com.victronenergy.switch.%s' % self._tty

		# Obtain the class and vrm instance from localsettings
		self.settings['deviceinstance'] =  ['/Settings/Devices/%s/ClassAndVrmInstance' % self._productNameSettings, "switch:50", "", ""]
		self.settings['customname'] = ['/Settings/Devices/%s/CustomName' % self._productNameSettings, "", "", ""]
		self._settings = self._create_settings(self.settings, self._handle_changed_setting)

		self.paths['/CustomName'] = {'value': self._settings['customname'], 'writeable': True, 'onchangecallback': self._handle_changed_value}
		self.paths['/Serial'] = {'value': self._serial, 'writeable': False}
		self.paths['/State'] = {'value': MODULE_STATE_CONNECTED, 'writeable': False, 'gettextcallback': self._module_state_text_callback}

		self._dbusService = self._create_dbus_service()

		for k, v in self.paths.items():
			value = v['value'] if 'value' in v else None
			writeable = v['writeable'] if 'writeable' in v else None
			onchangecallback = v['onchangecallback'] if 'onchangecallback' in v else None
			gettextcallback = v['gettextcallback'] if 'gettextcallback' in v else None
			self._dbusService.add_path(k, value=value, writeable=writeable, onchangecallback=onchangecallback, gettextcallback=gettextcallback)

		# Register on dbus
		self._dbusService.register()

	def add_output(self, channel, output_type, set_state_cb, name="", customName="", set_dimming_cb=None):
		path_base  = '/SwitchableOutput/%s/' % channel
		self.paths[path_base + 'State'] = {'value': 0, 'writeable': True, 'onchangecallback': set_state_cb}
		self.paths[path_base + 'Status'] = {'value': 0, 'writeable': False, 'gettextcallback': self._status_text_callback}
		self.paths[path_base + 'Name'] = {'value': name, 'writeable': False}

		if output_type == OUTPUT_TYPE_DIMMABLE:
			self.paths[path_base + "Dimming"] = {'value': 0, 'writeable': True, 'onchangecallback': set_dimming_cb, 'gettextcallback': lambda x, y: str(y) + '%'}

		# Settings
		validTypesDimmable = 1 << OUTPUT_TYPE_DIMMABLE
		validTypesToggle = 1 << OUTPUT_TYPE_TOGGLE
		validTypesMomentary = 1 << OUTPUT_TYPE_MOMENTARY

		self.paths[path_base + 'Settings/Group'] = {'value': "", 'writeable': True, 'onchangecallback': self._handle_changed_value}
		self.paths[path_base + 'Settings/CustomName'] = {'value': customName, 'writeable': True, 'onchangecallback': self._handle_changed_value}
		self.paths[path_base + 'Settings/ShowUIControl'] = {'value': 1, 'writeable': True, 'onchangecallback': self._handle_changed_value}
		self.paths[path_base + 'Settings/Type'] = {'value': output_type, 'writeable': True, 'onchangecallback': self._handle_changed_value,
							'gettextcallback': self._type_text_callback}
		self.paths[path_base + 'Settings/ValidTypes'] = {'value': validTypesDimmable if
							output_type == OUTPUT_TYPE_DIMMABLE else validTypesToggle | validTypesMomentary,
							'writeable': False, 'gettextcallback': self._valid_types_text_callback}
		self.paths[path_base + 'Settings/Function'] = {'value': OUTPUT_FUNCTION_MANUAL, 'writeable': True, 'onchangecallback': self._handle_changed_value,
							'gettextcallback': self._function_text_callback}
		self.paths[path_base + 'Settings/ValidFunctions'] = {'value': (1 << OUTPUT_FUNCTION_MANUAL), 
							'writeable': False, 'gettextcallback': self._valid_functions_text_callback}

	def _module_state_text_callback(self, path, value):
		if value == MODULE_STATE_CONNECTED:
			return "Connected"
		if value == MODULE_STATE_OVER_TEMPERATURE:
			return "Over temperature"
		if value == MODULE_STATE_TEMPERATURE_WARNING:
			return "Temperature warning"
		if value == MODULE_STATE_CHANNEL_FAULT:
			return "Channel fault"
		if value == MODULE_STATE_CHANNEL_TRIPPED:
			return "Channel tripped"
		if value == MODULE_STATE_UNDER_VOLTAGE:
			return "Under voltage"
		return "Unknown"

	def _status_text_callback(self, path, value):
		if value == 0x00:
			return "Off"
		if value == 0x09:
			return "On"
		if value == 0x02:
			return "Tripped"
		if value == 0x04:
			return "Over temperature"
		if value == 0x01:
			return "Powered"
		if value == 0x08:
			return "Output fault"
		if value == 0x10:
			return "Short fault"
		if value == 0x20:
			return "Disabled"
		return "Unknown"

	def _type_text_callback(self, path, value):
		if value == OUTPUT_TYPE_MOMENTARY:
			return "Momentary"
		if value == OUTPUT_TYPE_TOGGLE:
			return "Toggle"
		if value == OUTPUT_TYPE_DIMMABLE:
			return "Dimmable"
		return "Unknown"

	def _function_text_callback(self, path, value):
		if value == OUTPUT_FUNCTION_ALARM:
			return "Alarm"
		if value == OUTPUT_FUNCTION_GENSET_START_STOP:
			return "Genset start stop"
		if value == OUTPUT_FUNCTION_MANUAL:
			return "Manual"
		if value == OUTPUT_FUNCTION_TANK_PUMP:
			return "Tank pump"
		if value == OUTPUT_FUNCTION_TEMPERATURE:
			return "Temperature"
		if value == OUTPUT_FUNCTION_CONNECTED_GENSET_HELPER_RELAY:
			return "Connected genset helper relay"
		return "Unknown"

	def _valid_types_text_callback(self, path, value):
		str = ""
		if value & (1 << OUTPUT_TYPE_DIMMABLE):
			str += "Dimmable"
		if value & (1 << OUTPUT_TYPE_TOGGLE):
			if str:
				str += ", "
			str += "Toggle"
		if value & (1 << OUTPUT_TYPE_MOMENTARY):
			if str:
				str += ", "
			str += "Momentary"
		return str

	def _valid_functions_text_callback(self, path, value):
		str = ""
		if value & (1 << OUTPUT_FUNCTION_ALARM):
			str += "Alarm"
		if value & (1 << OUTPUT_FUNCTION_GENSET_START_STOP):
			if str:
				str += ", "
			str += "Genset start stop"
		if value & (1 << OUTPUT_FUNCTION_MANUAL):
			if str:
				str += ", "
			str += "Manual"
		if value & (1 << OUTPUT_FUNCTION_TANK_PUMP):
			if str:
				str += ", "
			str += "Tank pump"
		if value & (1 << OUTPUT_FUNCTION_TEMPERATURE):
			if str:
				str += ", "
			str += "Temperature"
		if value & (1 << OUTPUT_FUNCTION_CONNECTED_GENSET_HELPER_RELAY):
			if str:
				str += ", "
			str += "Connected genset helper relay"
		return str

	def _handle_changed_value(self, path, value):
		if path == '/CustomName':
			self._settings['customname'] = value

		return False

	def _handle_changed_setting(self, path, oldvalue, newvalue):
		if path == 'customname':
			self._dbusService['/CustomName'] = newvalue
			return True
		return False

	def _create_settings(self, *args, **kwargs):
		bus = dbus.Bus.get_session(private=True) if 'DBUS_SESSION_BUS_ADDRESS' \
				in os.environ else dbus.Bus.get_system(private=True)
		return SettingsDevice(bus, *args, timeout=10, **kwargs)

	def _create_dbus_service(self):
		bus = dbus.Bus.get_session(private=True) if 'DBUS_SESSION_BUS_ADDRESS' \
				in os.environ else dbus.Bus.get_system(private=True)

		dbusService = VeDbusService(self._serviceName, bus=bus, register=False)
		dbusService.add_mandatory_paths(
			processname=sys.argv[0],
			processversion=VERSION,
			connection=self._interface,
			deviceinstance=int(self._settings['deviceinstance'].split(':')[-1]),
			productid=self._productId,
			productname=self._productName,
			firmwareversion=VERSION,
			hardwareversion=None,
			connected=1)
		return dbusService

	def terminate(self, signum, frame):
		os._exit(0)

class GxIoExtender(SwitchingDevice):
	_productName = 'GX IO extender 150'
	_write_dimming_timer = None
	def __init__(self, serial):
		self._config_file = "/run/io-ext/{}/pins.conf".format(serial)
		self._check_config()
		self._serial = serial
		self.pins = self.parse_config(self._config_file)

		for pin in self.pins:
			output_type = pin.output_type
			channel = pin.name

			self.add_output(channel, output_type,
				   partial(self.set_hw_state, pin, 'State_%s' % channel),
				   name=pin.label,
				   set_dimming_cb=
				   partial(self.set_dimming, pin, 'Dimming_%s' % channel) if output_type == OUTPUT_TYPE_DIMMABLE else None)

			self.settings['Group_%s' % channel] = ['/Settings/Devices/gxio_%s/%s/Group' % (self._serial, channel), '', '', '']
			self.settings['CustomName_%s' % channel] = ['/Settings/Devices/gxio_%s/%s/CustomName' % (self._serial, channel), '', '', '']
			self.settings['ShowUIControl_%s' % channel] = ['/Settings/Devices/gxio_%s/%s/ShowUIControl' % (self._serial, channel), 1, 0, 1]
			self.settings['Type_%s' % channel] = ['/Settings/Devices/gxio_%s/%s/Type' % (self._serial, channel), output_type, 0, 2]
			# No setting for function, as it can only be manual.
			if pin.store_state:
				self.settings['State_%s' % channel] = ['/Settings/Devices/gxio_%s/%s/State' % (self._serial, channel), 0, 0, 1]

			if output_type == OUTPUT_TYPE_DIMMABLE:
				self.settings['Dimming_%s' % channel] = ['/Settings/Devices/gxio_%s/%s/Dimming' % (self._serial, channel), 0, 0, 255]

		super(GxIoExtender, self).__init__(PRODUCT_ID, tty=serial, interface="USB", serial=serial)

		for pin in self.pins:
			channel = pin.name
			# Set the initial state
			if pin.store_state:
				if pin.output_type == OUTPUT_TYPE_DIMMABLE:
					self._dbusService['/SwitchableOutput/%s/Dimming' % channel] = self._settings['Dimming_%s' % channel]
					pin.dimming = self._settings['Dimming_%s' % channel]
				self._dbusService['/SwitchableOutput/%s/State' % channel] = self._settings['State_%s' % channel]
				pin.state = self._settings['State_%s' % channel]
			else:
				self._dbusService['/SwitchableOutput/%s/State' % channel] = pin.state
			self._dbusService['/SwitchableOutput/%s/Status' % channel] = pin.status
			self._dbusService['/SwitchableOutput/%s/Settings/CustomName' % channel] = self._settings['CustomName_%s' % channel]
			self._dbusService['/SwitchableOutput/%s/Settings/Group' % channel] = self._settings['Group_%s' % channel]
			self._dbusService['/SwitchableOutput/%s/Settings/ShowUIControl' % channel] = self._settings['ShowUIControl_%s' % channel]
			self._dbusService['/SwitchableOutput/%s/Settings/Type' % channel] = self._settings['Type_%s' % channel]

	def terminate(self, signum, frame):
		if self._write_dimming_timer:
			GLib.source_remove(self._write_dimming_timer)
			self._write_dimming()
		return super().terminate(signum, frame)

	def _check_config(self):
		if os.path.exists(self._config_file):
			return True
		self.terminate(None, None)
		return

	def status_cb(self, pin):
		if self._dbusService:
			self._dbusService['/SwitchableOutput/%s/Status' % pin.name] = pin.status

	def set_hw_state(self, pin, setting, path, state):
		pin.state = state
		if pin.store_state:
			self._settings[setting] = pin.state
		return True

	def set_dimming(self, pin, setting, path, state):
		if state < 0 or state > 100:
			return False
		pin.dimming = state
		# Throttle writing the dimming state to localsettings.
		if self._write_dimming_timer:
			GLib.source_remove(self._write_dimming_timer)
			self._write_dimming_timer = None
		self._write_dimming_timer = GLib.timeout_add_seconds(1, self._write_dimming)
		return True

	def _write_dimming(self):
		for pin in [p for p in self.pins if p.output_type == OUTPUT_TYPE_DIMMABLE]:
			if pin.dimming != self._settings['Dimming_%s' % pin.name]:
				self._settings['Dimming_%s' % pin.name] = pin.dimming
		self._write_dimming_timer = None
		return False

	def _handle_changed_value(self, path, value):
		if path.endswith('/Type'):
			validTypesPath = path.replace('/Type', '/ValidTypes')
			if not ((1 << value) & self._dbusService[validTypesPath]):
				return False

			if int(value) == OUTPUT_TYPE_MOMENTARY:
				# Reset the state when the momentary type is selected
				self._dbusService[path.replace('/Settings/Type', '/State')] = 0
				# Call value changed directly to enforce the callback being called.
				self._dbusService._value_changed(
					'/SwitchableOutput/%s/State' % path.split('/')[-3], 0)

		elif path.endswith('/Function'):
			validFunctionsPath = path.replace('/Function', '/ValidFunctions')
			if not ((1 << value) & self._dbusService[validFunctionsPath]):
				return False

		split = path.split('/')
		if len(split) > 3 and split[3] == 'Settings':
			setting = split[-1] + '_' + split[-3]
			try:
				self._settings[setting] = value
			except KeyError:
				return False
			return True

		return super(GxIoExtender, self)._handle_changed_value(path, value)

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
	parser.add_argument('-d', '--debug', help='set logging level to debug',
						action='store_true')
	args = parser.parse_args()

	print('-------- dbus-switch, v' + VERSION + ' is starting up --------')

	logger = setup_logging(args.debug)

	# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
	DBusGMainLoop(set_as_default=True)

	ioExtender = GxIoExtender(args.serial)
	signal.signal(signal.SIGTERM, ioExtender.terminate)
	signal.signal(signal.SIGINT, ioExtender.terminate)

	# Start and run the mainloop
	mainloop = GLib.MainLoop()
	mainloop.run()
