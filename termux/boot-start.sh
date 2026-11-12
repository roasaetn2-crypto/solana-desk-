#!/data/data/com.termux/files/usr/bin/bash
# Termux:Boot hook — starts the desk automatically after the phone reboots.
#
# Install:
#   1. Install "Termux:Boot" from F-Droid (same source as Termux).
#   2. Open Termux:Boot once so it registers itself.
#   3. mkdir -p ~/.termux/boot
#   4. cp ~/solana-desk/termux/boot-start.sh ~/.termux/boot/10-desk.sh
#   5. chmod +x ~/.termux/boot/10-desk.sh
#
# After a reboot, Android runs this and the desk comes back on its own.
termux-wake-lock 2>/dev/null || true
sleep 20   # give the network a moment to come up
exec bash "$HOME/solana-desk/termux/start-desk.sh"
