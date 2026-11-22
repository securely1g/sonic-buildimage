ifeq ($(CONFIGURED_PLATFORM):$(BLDENV):$(MAKECMDGOALS):$(SONIC_DIAG_NATIVE_REPLAY),vs:trixie:target/sonic-vs.bin:)
# Hold this user, environment and native builder for all measured installer invocations.
vs_host_benchmark_daemon := $(shell python3 -B /sonic/.configured-host-bench/inside.py capture)
$(error configured-host benchmark builder capture ended)
endif
