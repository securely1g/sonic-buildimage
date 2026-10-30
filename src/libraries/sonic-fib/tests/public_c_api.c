/* Exercise the installed C header names and the shared ABI used by FRR. */
#include <stdbool.h>
#include <stdint.h>
#include <stdarg.h>
#include <string.h>
#include <nexthopgroup/c_nexthopgroupfull.h>
#include <nexthopgroup/c-api/nexthopgroup_capi.h>

int main(void)
{
    if (strcmp(nexthopgroup_version(), "1.0.0") != 0)
        return 1;
    fib_frr_set_log_level(3);
    if (fib_frr_get_log_level() != 3)
        return 2;
    fib_frr_set_log_level(0);
    return fib_frr_get_log_level() != 0;
}
