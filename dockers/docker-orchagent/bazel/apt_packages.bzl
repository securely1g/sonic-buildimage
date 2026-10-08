"""Packages added to the image; dependencies supplied by its base stay there.

These are public rules_distroless package labels. The shared checker reads
:data and :control without pulling in the root target's dependency closure.
"""

RUNTIME_APT_PACKAGES = [
    "@orchagent_debian//arping",
    "@orchagent_debian//bridge-utils",
    "@orchagent_debian//conntrack",
    "@orchagent_debian//ifupdown",
    "@orchagent_debian//iputils-ping",
    "@orchagent_debian//libkmod2",
    "@orchagent_debian//libnet1",
    "@orchagent_debian//libnetfilter-conntrack3",
    "@orchagent_debian//libnfnetlink0",
    "@orchagent_debian//libpcap0.8t64",
    "@orchagent_debian//libpci3",
    "@orchagent_debian//libprotobuf-lite32t64",
    "@orchagent_debian//libprotobuf32t64",
    "@orchagent_debian//libteam5",
    "@orchagent_debian//libteamdctl0",
    "@orchagent_debian//libxml2",
    "@orchagent_debian//libyaml-cpp0.8",
    "@orchagent_debian//ndisc6",
    "@orchagent_debian//ndppd",
    "@orchagent_debian//pci.ids",
    "@orchagent_debian//pciutils",
    "@orchagent_debian//python3-click",
    "@orchagent_debian//python3-netifaces",
    "@orchagent_debian//python3-protobuf",
    "@orchagent_debian//tcpdump",
]

DEBUG_APT_PACKAGES = [
    "@orchagent_debug_debian//libngtcp2-16",
    "@orchagent_debug_debian//libngtcp2-crypto-gnutls8",
    "@orchagent_debug_debian//gdb",
    "@orchagent_debug_debian//gdbserver",
    "@orchagent_debug_debian//libbabeltrace1",
    "@orchagent_debug_debian//libcurl3t64-gnutls",
    "@orchagent_debug_debian//libdebuginfod-common",
    "@orchagent_debug_debian//libdebuginfod1t64",
    "@orchagent_debug_debian//libdw1t64",
    "@orchagent_debug_debian//libglib2.0-0t64",
    "@orchagent_debug_debian//libipt2",
    "@orchagent_debug_debian//libjson-c5",
    "@orchagent_debug_debian//libmpfr6",
    "@orchagent_debug_debian//libsource-highlight-common",
    "@orchagent_debug_debian//libsource-highlight4t64",
    "@orchagent_debug_debian//libtext-charwidth-perl",
    "@orchagent_debug_debian//libtext-wrapi18n-perl",
    "@orchagent_debug_debian//libunwind8",
    "@orchagent_debug_debian//sensible-utils",
    "@orchagent_debug_debian//strace",
    "@orchagent_debug_debian//ucf",
]
