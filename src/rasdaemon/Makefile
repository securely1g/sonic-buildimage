.ONESHELL:
SHELL = /bin/bash
.SHELLFLAGS += -e

MAIN_TARGET = rasdaemon_$(RASDAEMON_VERSION)_$(CONFIGURED_ARCH).deb
DERIVED_TARGETS = rasdaemon-dbgsym_$(RASDAEMON_VERSION)_$(CONFIGURED_ARCH).deb

# Pinned commit hash for debian/0.6.8-1 tag, used by ordinary Make builds.
RASDAEMON_COMMIT = 51a7f485f8b2e2ae43e613f19c5a387595174132

# Source CI can use the authenticated Debian source release when Salsa is
# unavailable. Keep the default source and revision unchanged.
RASDAEMON_SOURCE_METHOD ?= git
ifeq ($(RASDAEMON_SOURCE_METHOD),debian)
ifneq ($(RASDAEMON_VERSION),0.6.8-1)
$(error Debian source hashes are pinned to RASDAEMON_VERSION=0.6.8-1)
endif
ifeq ($(CROSS_BUILD_ENVIRON),y)
$(error RASDAEMON_SOURCE_METHOD=debian supports native builds only)
endif
else ifneq ($(RASDAEMON_SOURCE_METHOD),git)
$(error Unsupported RASDAEMON_SOURCE_METHOD=$(RASDAEMON_SOURCE_METHOD))
endif

$(addprefix $(DEST)/, $(MAIN_TARGET)): $(DEST)/% :
	rm -rf rasdaemon/

ifeq ($(RASDAEMON_SOURCE_METHOD),debian)
	# SHA256 hashes authenticated by Debian sid's signed Sources index from
	# snapshot 20230215T000000Z. This verifies the 0.6.8-1 source release;
	# byte-for-byte equivalence with the Salsa Git tree is not asserted.
	wget -q -O rasdaemon_0.6.8-1.dsc https://snapshot.debian.org/file/9edba1922916443b41c41f6398c76bda19eff02b
	wget -q -O rasdaemon_0.6.8.orig.tar.gz https://snapshot.debian.org/file/a6f8bb611222ae9b1b60f0c62d4b02dbc81e15b7
	wget -q -O rasdaemon_0.6.8-1.debian.tar.xz https://snapshot.debian.org/file/49b109ee889df17c3ece6c754db7609a8dbeb699
	echo '50009d1318f897f01f24264511b2c7f90a5626447b40e42b488fedd54c750da9  rasdaemon_0.6.8-1.dsc' | sha256sum -c -
	echo 'd7c9ad61a13ca230e376db4014ae3192992eff10908adf38fad1917609f7d341  rasdaemon_0.6.8.orig.tar.gz' | sha256sum -c -
	echo '651e13bf4bfb489dce0f427a46bf2ee015548c30fcfec32c544d98806225e63f  rasdaemon_0.6.8-1.debian.tar.xz' | sha256sum -c -
	# All source files are checked above; extraction needs no maintainer keyring.
	dpkg-source --no-check -x rasdaemon_0.6.8-1.dsc ./rasdaemon
	test "$$(dpkg-parsechangelog -lrasdaemon/debian/changelog -SVersion)" = "$(RASDAEMON_VERSION)"
	pushd ./rasdaemon
	# Keep git apply relative to this package, not the enclosing buildimage repo.
	git init -q
else
	# Checkout Repository (pinned to specific commit for reproducibility)
	git clone https://salsa.debian.org/tai271828/rasdaemon.git
	pushd ./rasdaemon
	git checkout $(RASDAEMON_COMMIT)
endif

	# Patch
	git apply ../0001-Check-CPUs-online-not-configured.patch
ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -rfakeroot -b -us -uc -a$(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -rfakeroot -b -us -uc -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd

	mv $(DERIVED_TARGETS) $* $(DEST)/

$(addprefix $(DEST)/, $(DERIVED_TARGETS)): $(DEST)/% : $(DEST)/$(MAIN_TARGET)
