# DPU-2P APP_DB port startup race theory

## Summary

The observed `test_dash_acl` failure on DPU-2P VS is likely caused by a startup/order race in the SWSS container, not by a static DPU platform configuration mismatch.

`portsyncd` can start before `configdb-load` has populated `CONFIG_DB|PORT`. In that window it sees zero ConfigDB ports and still publishes port initialization completion into APP_DB. `orchagent` then starts and reconciles SAI-discovered ports against an empty or incomplete APP_DB port table.

## Observed failure sequence

From the failing `test_dash_acl` CI log bundle:

1. `portsyncd` starts early and queries ConfigDB.
2. ConfigDB has no `PORT` information yet.
3. `portsyncd` continues and publishes completion state:
   - `PORT_TABLE:PortInitDone`
   - `PORT_TABLE:PortConfigDone` with `count:0`
4. `orchagent` starts after the false completion state.
5. SAI discovery is correct: two DPU ports exist with lane sets:
   - `0,1,2,3`
   - `4,5,6,7`
6. The real DPU ports appear in APP_DB only later:
   - `PORT_TABLE:Ethernet0`
   - `PORT_TABLE:Ethernet4`
7. By then, `orchagent` has already consumed an empty or partial port view and removes one of the discovered SAI ports.
8. `syncd` then fails while collecting related objects for the removed port.

Representative CI log evidence:

```text
portsyncd: handlePortConfigFromConfigDB: Getting port configuration from ConfigDB...
portsyncd: handlePortConfigFromConfigDB: ConfigDB does not have port information, however ports can be added later on, continuing...
portsyncd: main: PortInitDone
```

```text
PORT_TABLE:PortInitDone|SET|lanes:0
PORT_TABLE:PortConfigDone|SET|count:0
...
PORT_TABLE:Ethernet4|SET|alias:etp2|index:2|lanes:4,5,6,7|speed:100000|subport:0|mtu:9100|admin_status:down
PORT_TABLE:Ethernet0|SET|alias:etp1|index:1|lanes:0,1,2,3|speed:100000|subport:0|mtu:9100|admin_status:down
```

```text
orchagent: initializePorts: Get 2 ports
orchagent: initializePorts: Get port with lanes pid:1000000000002 lanes:0 1 2 3
orchagent: initializePorts: Get port with lanes pid:1000000000003 lanes:4 5 6 7
```

```text
sairedis.rec: R|SAI_OBJECT_TYPE_PORT||oid:0x1000000000003
syncd: collectPortRelatedObjects: failed to obtain related objects for port rid oid:0x100000002: SAI_STATUS_NOT_IMPLEMENTED, attr id: 5
```

## Local reproduction notes

A direct `docker run` of the VS image can be harsher than DVS because if `eth1`/`eth2` are not present before `start.sh`, the startup script filters `lanemap.ini` and `port_config.ini` to empty. To reproduce the DPU-2P shape, the container needs front-panel-like interfaces attached before `start.sh` runs.

With `eth1` and `eth2` present before startup, the generated DPU files are correct:

```text
lanemap.ini:
eth1:0,1,2,3
eth2:4,5,6,7

port_config.ini:
Ethernet0  0,1,2,3  etp1  1
Ethernet4  4,5,6,7  etp2  2
```

This confirms the static config path can produce the expected DPU-2P ports. The failure is caused by when APP_DB completion is published relative to config loading and port publication.

## Working theory

`PortInitDone` / `PortConfigDone` should not be published with zero ports for this cold-start DPU-2P path before ConfigDB has had a chance to load the generated port configuration.

Potential fixes to evaluate:

- Make `portsyncd` wait for ConfigDB port data, or at least avoid publishing final done state when ConfigDB is empty during cold start.
- Adjust SWSS startup dependencies so `portsyncd`/`orchagent` do not race ahead of config loading for VS DPU-2P.
- Make `orchagent` robust against receiving `PortConfigDone count:0` followed by real ports, so it does not reconcile/remove discovered SAI ports prematurely.

## Related packaging issue

The failing image also exposed an independent issue: raw `syncd` startup failed in one downloaded image because `libasan.so.8` was missing. A local probe image with `libasan8` installed was used to get past that and observe the DPU-2P port race.
