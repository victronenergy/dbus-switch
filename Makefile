INSTALL_CMD = install
LIBDIR = $(bindir)/ext/velib_python

FILES = \
	dbus-switch.py \

VELIB_FILES = \
	ext/velib_python/logger.py \
	ext/velib_python/settingsdevice.py \
	ext/velib_python/ve_utils.py \
	ext/velib_python/vedbus.py

compile: ;

install_app : $(FILES)
	@if [ "$^" != "" ]; then \
		$(INSTALL_CMD) -m 755 -d $(DESTDIR)$(bindir); \
		$(INSTALL_CMD) -m 755 -t $(DESTDIR)$(bindir) $^; \
		echo installed $(DESTDIR)$(bindir)/$(notdir $^); \
	fi

install_velib_python: $(VELIB_FILES)
	@if [ "$^" != "" ]; then \
		$(INSTALL_CMD) -m 755 -d $(DESTDIR)$(LIBDIR); \
		$(INSTALL_CMD) -m 644 -t $(DESTDIR)$(LIBDIR) $^; \
		echo installed $(DESTDIR)$(LIBDIR)/$(notdir $^); \
	fi

install: install_velib_python install_app

clean distclean: ;

testinstall:
	$(eval TMP := $(shell mktemp -d))
	$(MAKE) DESTDIR=$(TMP) install
	(cd $(TMP) && ./dbus-switch.py)

.PHONY: compile install clean distclean testinstall
