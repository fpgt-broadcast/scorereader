# Hardware

Ways to get a scoreboard console's data into ScoreReader without a Sportzcast ScoreBot:

| Option | Inputs | Connects by | Cost | Status |
|---|---|---|---|---|
| [ScoreBridge](scorebridge/README.md): our own board and case (WT32-ETH01) | Current loop, RS-232, RS-485/422 | Ethernet, Wi-Fi or USB | ~$35–50 | v1.0 design, not built yet |
| [Nano bridge](nano-bridge/README.md): Arduino Nano + optocoupler | Current loop | USB | a few dollars | sketch, not tested on hardware |
| Plain USB-RS232 adapter | RS-232 (Colorado Timing, Daktronics J6 RTD) | USB | ~$20 | works now: Source = Serial |

The ScoreBot stays the only option for brands its own chip decodes: Nevco, Daktronics All Sport 4000 and Fair-Play MP-69/70.
