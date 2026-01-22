# dbus_shelly/utils.py
import asyncio
import logging
from aiovelib.client import Monitor
from aiovelib.localsettings import SettingsService as SettingsClient, SETTINGS_SERVICE

STATUS_OFF = 0x00
STATUS_ON = 0x09

# Text formatters
formatters = {
	'watt': lambda v: "{:.0f}W".format(v),
	'volt': lambda v: "{:.1f}V".format(v),
	'amp': lambda v: "{:.1f}A".format(v),
	'kwh': lambda v: "{:.2f}kWh".format(v),
	'productid': lambda v: "0x{:X}".format(v)
}

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class SettingsMonitor(Monitor):
	def __init__(self, bus, **kwargs):
		super().__init__(bus, handlers = {
			'com.victronenergy.settings': SettingsClient
		}, **kwargs)
		

async def wait_for_settings(bus, itemsChanged=None):
	""" Attempt a connection to localsettings. """
	settingsmonitor = await SettingsMonitor.create(bus, itemsChanged=itemsChanged)
	""" Attempt a connection to localsettings. If it does not show
		    up within 5 seconds, return None. """
	try:
		return await asyncio.wait_for(
			settingsmonitor.wait_for_service(SETTINGS_SERVICE), 5)
	except TimeoutError:
		pass

	return None