.ONESHELL:
SHELL = /bin/bash
.SHELLFLAGS += -e

MAIN_TARGET = monit_$(MONIT_VERSION)_$(CONFIGURED_ARCH).deb
DERIVED_TARGETS = monit-dbgsym_$(MONIT_VERSION)_$(CONFIGURED_ARCH).deb

# Source CI can use Debian's authenticated release instead of Salsa Git.
# Keep ordinary Make builds on the existing Git path.
MONIT_SOURCE_METHOD ?= git
ifeq ($(MONIT_SOURCE_METHOD),debian)
ifneq ($(MONIT_VERSION),5.34.3-1)
$(error Debian source hashes are pinned to MONIT_VERSION=5.34.3-1)
endif
ifeq ($(CROSS_BUILD_ENVIRON),y)
$(error MONIT_SOURCE_METHOD=debian supports native builds only)
endif
else ifneq ($(MONIT_SOURCE_METHOD),git)
$(error Unsupported MONIT_SOURCE_METHOD=$(MONIT_SOURCE_METHOD))
endif

$(addprefix $(DEST)/, $(MAIN_TARGET)): $(DEST)/% :
	# Remove any stale files
	rm -rf ./monit

ifeq ($(MONIT_SOURCE_METHOD),debian)
	# These hashes match Debian Trixie's signed Sources index for 1:5.34.3-1.
	wget -q -O monit_5.34.3-1.dsc https://deb.debian.org/debian/pool/main/m/monit/monit_5.34.3-1.dsc
	wget -q -O monit_5.34.3.orig.tar.gz https://deb.debian.org/debian/pool/main/m/monit/monit_5.34.3.orig.tar.gz
	wget -q -O monit_5.34.3-1.debian.tar.xz https://deb.debian.org/debian/pool/main/m/monit/monit_5.34.3-1.debian.tar.xz
	echo '6339314234af0d8dffac62364c018dd547a4d21010e9ff76d761f15671138fe8  monit_5.34.3-1.dsc' | sha256sum -c -
	echo '669d8b95ddec124d1444ba5264f67fdeae8e90e53b2929719f4750fc5ff3ba60  monit_5.34.3.orig.tar.gz' | sha256sum -c -
	echo '2f314b1774c4e2351c71fb05ab988cece0cad458dab7a1bf236cc82ddbf97cda  monit_5.34.3-1.debian.tar.xz' | sha256sum -c -
	# All source files are checked above; extraction needs no maintainer keyring.
	dpkg-source --no-check -x monit_5.34.3-1.dsc ./monit
	pushd ./monit
	# stg needs a local Git baseline even when source arrives as an archive.
	git init
	git add .
	git -c commit.gpgsign=false commit -qm 'Import Debian Monit 1:5.34.3-1 source'
else
	# Clone monit repo
	git clone https://salsa.debian.org/debian/monit.git
	pushd ./monit

	# Reset HEAD to the commit of the proper tag
	# NOTE: Using "git checkout <tag_name>" here detaches our HEAD,
	# which stg doesn't like, so we use this method instead
	# NOTE: For some reason, tags in the Debian monit repo are prefixed with "1%"
	git reset --hard debian/1\%$(MONIT_VERSION)
endif

	# Apply patches
	stg init
	stg import -s ../patch/series

ifeq ($(CROSS_BUILD_ENVIRON), y)
	patch -p1 < ../patch/cross-compile-changes.patch
endif

	# Build source and Debian packages
ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -rfakeroot -b -us -uc --host-arch $(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -rfakeroot -b -us -uc -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd

	# Move the newly-built .deb packages to the destination directory
	mv $(DERIVED_TARGETS) $* $(DEST)/

$(addprefix $(DEST)/, $(DERIVED_TARGETS)): $(DEST)/% : $(DEST)/$(MAIN_TARGET)
