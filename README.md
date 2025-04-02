# dbus-switch

Python script for interfacing with digital switching devices over the dbus. The script creates one instance of `com.victronenergy.switch` ([dbus specification](https://github.com/victronenergy/venus/wiki/dbus#switch)) on the dbus. 

The script currently only supports the GX IO Extender 150. The digital inputs of the IO extender are handled by [dbus-digitalinputs](https://github.com/victronenergy/dbus-digitalinputs)