# Turning on ADB on a Mi TV (Xiaomi / PatchWall)

This is the only fiddly part. Once `adb devices` shows your TV, everything else
is one command.

## 1. Same network

TV and computer must be on the same Wi-Fi/LAN. Guest networks and AP isolation
block port 5555 and look identical to "it just doesn't work".

## 2. Note the TV's IP

TV: **Settings → Network & Internet → (your network) → IP address**

It looks like `192.168.1.42`. You need it in step 5.

## 3. Enable Developer options

TV: **Settings → Device Preferences → About → Build**

Highlight **Build** and press OK **7 times**. You should see "You are now a
developer".

> On some Mi TV generations the path is **Settings → About → Build**, or
> **Settings → System → About**. Look for the entry named `Build` — not
> "Android version".

## 4. Enable debugging

TV: **Settings → Device Preferences → Developer options**

- Turn on **USB debugging**
- If present, also turn on **ADB debugging** / **Network debugging**
- Leave **Verify apps over USB** alone

## 5. Connect

On the computer:

```bash
adb connect 192.168.1.42:5555
adb devices
```

First connection pops a dialog on the TV:

> Allow USB debugging?
> The computer's RSA key fingerprint is …

Tick **Always allow from this computer**, then **OK**.

`adb devices` should now show:

```
192.168.1.42:5555    device
```

If it says `unauthorized`, you missed the dialog. Unplug/replug with
`adb disconnect && adb connect ...` and watch the TV screen.

## 6. If port 5555 is closed

Some Mi TV firmware does not open TCP debugging until it has seen a USB
connection once.

1. Connect a USB-A to USB-A cable (or USB-C/USB-A adapter) between TV and
   computer. Not every Mi TV accepts this; the service port is usually the one
   next to HDMI 1.
2. `adb devices` — the TV appears as a USB device.
3. `adb tcpip 5555`
4. Unplug, then `adb connect <TV_IP>:5555`.

## 7. Android 11+ TVs (wireless debugging pairing)

Newer builds require a pairing code:

TV: **Developer options → Wireless debugging → Pair device with pairing code**

```
IP address & Port:  192.168.1.42:37123
Wi-Fi pairing code: 123456
```

```bash
adb pair 192.168.1.42:37123      # enter the 6-digit code
adb connect 192.168.1.42:5555
```

## 8. Keep it working

- The TV must stay awake. If it sleeps, `adb devices` goes empty; wake it and
  reconnect.
- The debugging toggle sometimes turns itself off after a firmware update.
  Re-check step 4 if `connect` starts failing.
- Your computer's ADB key lives in `~/.android/adbkey`. Delete it only if you
  want to re-approve from scratch.

## Recovery if the TV misbehaves

1. **Wait and reboot** — most removals only take effect on next launch.
2. `mitv restore --last` from the computer, then reboot.
3. If Wi-Fi debugging is unreachable, use a USB keyboard/mouse and
   **Settings → Device Preferences → Reset → Factory reset**. A factory reset
   restores every per-user removal, because nothing was deleted from `/system`.

That last point is the reason this tool never touches `/system`.
