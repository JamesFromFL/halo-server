# Subscription-free Ring Alarm control: feasibility

Research date: November 14, 2026. Documentation/source research only. No account
entitlement tests, subscription changes, alarm writes, resets or device migrations.

## Conclusion

There is no demonstrated general software-only route in the reviewed integrations
for digitally arming a subscription-gated Ring base station without the required
account entitlement. Their cloud transports still ask Ring to perform the action.
Changing the user interface to HomeKit, MQTT or Halo does not make those requests
local. This is an evidence-based engineering conclusion, not a proof that every
possible firmware, radio or future interface is impossible.

Three different propositions must stay separate:

1. **Use a Ring alarm without paying:** physical keypad operation and local siren
   behavior are documented for the current US offering.
2. **Control that same Ring alarm digitally without paying:** possible only where
   the account/device/region actually permits it, including applicable legacy
   cases; no universal entitlement bypass was established.
3. **Use compatible Ring-branded peripherals in a subscription-free local alarm:**
   technically plausible using a separate Z-Wave controller and local alarm engine.
   That changes the alarm controller rather than unlocking Ring's cloud service.

## Primary evidence, date and scope

- Ring's current [US plan guidance](https://ring.com/support/articles/47t1b/Which-Ring-Alarm-Plan-Is-Right-for-Me)
  explicitly labels itself US-only and separates subscribed digital control from
  unsubscribed keypad/local-siren use. It also distinguishes self-monitoring from
  professional monitoring. A paid dispatch service is not synonymous with the
  entitlement for digital control.
- The [current US arming article](https://ring.com/support/articles/8fscc/Arming-Disarming-and-Setting-Modes-on-your-Ring-Alarm)
  retains the compatible-subscription footnote for digital arming/disarming.
- In [ring-mqtt discussion 912](https://github.com/tsightler/ring-mqtt/discussions/912),
  maintainer tsightler explains on September 29, 2024 that devices purchased after
  March 29, 2023 require a subscription for digital arming, including ring-mqtt.
  The same answer explains that the generic failed-mode message can misleadingly
  suggest sensor bypass even when entitlement is the real issue.
- Ring's [2023 staff announcement](https://community.ring.com/t/ring-alarm-subscription-changes/189243/35)
  described grandfathered US/Canada devices predating March 29, 2023. The original
  URL is still search-indexed but failed direct opening in this research. The old
  support article now redirects to [current plans](https://ring.com/plans).
  [This Ring support discussion](https://community.ring.com/en_GB/conversations/ring-alarm/is-inapp-feature-not-available-for-existing-customers/658042e551f6e6fe786c30fd)
  reproduces the historical policy and a staff response directing account-specific
  entitlement disputes to support. Historical policy is not current verification
  of any owner's entitlement or a guarantee that a second-hand/replacement unit
  inherits it. Do not confuse this with grandfathered subscription pricing.
- Regional behavior differs. [Ring staff's June 2023 UK clarification](https://community.ring.com/en_GB/conversations/ringalarm/remote-armingdisarming/6580437351f6e6fe78742d54)
  explicitly says the US/Canada change did not apply to UK app arming. The
  [current GB arming article](https://ring.com/gb/en/support/articles/8fscc/Arming-Disarming-and-Setting-Modes-on-your-Ring-Alarm)
  describes app and keypad modes, with its subscription footnote attached to
  Alarm Calls instead. This supports region-aware capability handling; it is not
  evidence that changing a region setting grants a legitimate entitlement.

## Options and recommendation

| Option | Feasibility for Halo | Limits / decision |
| --- | --- | --- |
| Physical Ring keypad with existing base | Documented subscription-free US operation | Useful manual fallback; not remote HomeKit/Halo arming. |
| Existing permitted legacy or regional cloud account | Conditional | Halo should use the account's actual permitted capabilities. Verify source state and explicit permission/denial; no hardcoded universal paid-plan gate. |
| Alternate cloud client: Scrypted/Homebridge/ring-mqtt/Halo | No verified bypass for a gated account | These reuse cloud APIs. A successful connection, discovery or sensor read does not prove an arm command is permitted. |
| Ring base on local LAN / VPN | No supported arm/disarm endpoint identified in the projects reviewed | VPN reachability cannot create a local protocol. Do not equate local server hosting or a WebSocket with local base-station control. |
| Base-station Z-Wave learn mode / secondary controller | Research-only lead; not an established arm/disarm solution | Ring's technical manual documents network membership/replication, not a supported external alarm-control command. Learn mode has device-membership constraints. Not part of normal Halo installation. |
| Direct local Z-Wave sensors/keypad plus Alarmo or equivalent | Credible separate project for compatible devices | Requires radio hardware, supported firmware, migration/re-inclusion and a local alarm engine. Moved peripherals are no longer managed by the original Ring network. Camera integration can remain unchanged. |
| Custom keypad hardware/emulation | Speculative | The physical keypad proves a local authenticated path exists, but this review found no qualified drop-in implementation controlling the existing base. Requires spare hardware, protocol and failure-state research. Not a release commitment. |
| Mechanical keypad actuator | Physically conceivable, poor product fit | Added hardware, code entry and uncertain observed result; not a reliable general Halo feature. No build recommended. |

Ring's [second-generation keypad technical manual](https://assets.ctfassets.net/jrz4hnnvdyct/1KyDycUkzZxwEwFzri7u0J/a9bb825c829aa6e136161257366aaedf/Ring_Alarm_Keypad_Zwave_NA.pdf)
documents cross-vendor Z-Wave operation, authenticated S2 inclusion, Entry Control
and Indicator commands. This supports the local-peripheral route, not a general
promise for every Ring product. The local controller must implement code handling,
mode indications, entry/exit delays, siren actions and unavailable-node behavior.

The [base-station Z-Wave manual](https://products.z-wavealliance.org/wp-content/uploads/products/52721/Base%20Station%20ZWave%20Technical%20Manual.pdf)
documents learn mode only when the base has no paired devices and warns that a
network reset removes its devices. Therefore a pairing experiment on the owner's
installed security system is not a harmless discovery test. No such action was
performed or is part of the bridge plan.

A separate local alarm would need its own power/reboot strategy, durable armed
state, physical siren and keypad acceptance tests, reliable notifications and
remote access. It does not retain Ring professional dispatch or cellular backup
merely by reusing Ring peripherals. This is a new optional backend/product scope
requiring its own design decision, not a hidden change to the current plan.

## Changes required in Halo's design

- Maintain capability evidence separately for reading state, receiving events,
  and writing each command. Entitlement is per account/location/feature, not a
  single `has_subscription` boolean inferred from a package name.
- Represent permitted, explicitly denied and unknown separately. Check evidence
  on connection/account changes; do not issue test arm/disarm writes at startup
  to discover permissions.
- Explain known role/entitlement denial accurately. An ambiguous timeout is not
  automatically "subscription required" or "sensor bypass required".
- Preserve read-only services when control is denied but readings remain
  available; don't assume readings are universally available without a plan.
- Never claim an alarm is armed merely because a HomeKit target changed. Require
  authoritative mode evidence and retain entry/exit-delay/trouble state.
- No subscription cancellation, region spoofing, account changes or alarm commands
  were needed for this research. Any later owner's-account test must distinguish
  actual non-subscriber evidence from a still-active trial or paid entitlement.

The recommended current scope is the Ring bridge, with accurate entitlement
handling and regional/legacy support where available. Record a separate optional
local-alarm provider as a future route if guaranteed independence from Ring
subscription policies becomes a firm product requirement.
