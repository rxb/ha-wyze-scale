# Wyze Scale X BLE Protocol

This document describes the Bluetooth Low Energy protocol used by the Wyze
Scale X (advertised name `WL_SC3`), as reverse-engineered from observed
behavior. It is sufficient to implement a client in any language.

The protocol is layered:

1. **BLE/GATT transport** — a single characteristic used for both writes and
   notifications, with a small outer frame header.
2. **Encryption layer** — a Diffie-Hellman key exchange followed by XXTEA
   encryption of all subsequent traffic, in independent 8-byte blocks.
3. **Application layer** — command/reply messages carried inside the
   encrypted payload.

All multi-byte integers are **little-endian** unless stated otherwise.

---

## 1. Discovery and GATT

### Advertisement

The scale advertises:

- **Local name:** `WL_SC3`
- **Advertised service UUID:** `0000fd7b-0000-1000-8000-00805f9b34fb`
- **Manufacturer data:** a single entry with company ID `0x0870`, whose
  payload begins with the bytes `07 02`.

Scanning for the service UUID `0000FD7B` is the most reliable discovery
filter.

### GATT service and characteristic

| Item | UUID |
|---|---|
| Service | `0000fd7b-0000-1000-8000-00805f9b34fb` |
| Characteristic | `00000001-0000-1000-8000-00805f9b34fb` |

All communication uses this **one characteristic**:

- The client **writes** requests to it.
- The scale responds and pushes data via **notifications** on it.

The client must enable notifications immediately after connecting, before
sending anything.

**MTU note:** frames can be up to ~60 bytes (and user-list notifications
larger), so negotiate an ATT MTU well above the 23-byte default (e.g. 247).

---

## 2. Transport framing

Every write and every notification begins with a header byte whose **high
nibble identifies the frame type** and whose **low nibble is a 4-bit rolling
frame counter**:

| High nibble | Direction | Meaning |
|---|---|---|
| `0x0` | client → scale | Key-exchange (plaintext control) message |
| `0x1` | client → scale | Encrypted data message |
| `0x4` | scale → client | Key-exchange reply |
| `0x5` | scale → client | Encrypted data message |

The client maintains a single frame counter starting at **0** on each new
connection, incremented by 1 for every message it sends (both key-exchange
and encrypted messages share the counter), wrapping modulo 16. The scale is
not observed to reject out-of-order counters, but a fresh connection should
start at 0. The counter in the scale's frames can be ignored.

---

## 3. Encryption layer

### 3.1 Key exchange (first message after connecting)

A classic Diffie-Hellman exchange over a 32-bit prime field:

- Generator (base): `5`
- Modulus: `0xFFFFFFC5` (4294967237)

The client:

1. Generates a random 32-bit private key `a`.
2. Computes its public key `A = 5^a mod 0xFFFFFFC5`.
3. Sends the key-exchange request below.

**Key-exchange request (client → scale), 12 bytes:**

| Offset | Size | Value |
|---|---|---|
| 0 | 1 | Frame header: high nibble `0x0`, low nibble = frame counter (first message, so typically `0x00`) |
| 1 | 1 | `0xF0` (key-exchange message type) |
| 2–3 | 2 | Payload length, big-endian: `0x00 0x08` |
| 4–7 | 4 | Client public key `A`, uint32 LE |
| 8–11 | 4 | `0x00 0x00 0x00 0x00` |

**Key-exchange reply (scale → client), 12 bytes, identical layout:**

| Offset | Size | Value |
|---|---|---|
| 0 | 1 | High nibble `0x4`, low nibble = scale frame counter |
| 1–3 | 3 | `0xF0 0x00 0x08` |
| 4–7 | 4 | Scale public key `B`, uint32 LE |
| 8–11 | 4 | zeros |

The client computes the shared secret `S = B^a mod 0xFFFFFFC5`.

### 3.2 Session key derivation

The 128-bit XXTEA key is derived from the shared secret `S` as:

1. Format `S` as **exactly 8 lowercase hexadecimal ASCII characters**,
   zero-padded (e.g. `S = 0x0AB3` → the string `"00000ab3"`).
2. The 16-byte key is those 8 ASCII bytes followed by **8 zero bytes**.

Example: `S = 0x89ABCDEF` → key =
`38 39 61 62 63 64 65 66 00 00 00 00 00 00 00 00`.

### 3.3 Block cipher

All post-handshake payloads are encrypted with **XXTEA (Corrected Block
TEA)** using the 16-byte key above, applied **independently to each 8-byte
block** (i.e. ECB-style, block size fixed at 8 bytes, no chaining, no
padding inside the cipher):

- Each 8-byte block is interpreted as **two little-endian uint32 words**
  (`n = 2`).
- The key is interpreted as **four little-endian uint32 words**.
- Standard XXTEA parameters: `DELTA = 0x9E3779B9`, number of mixing rounds
  `q = 6 + 52/n = 32`.

### 3.4 Encrypted frame format

**Client → scale:**

| Offset | Size | Value |
|---|---|---|
| 0 | 1 | `0x10 + frame_counter` (high nibble `0x1`) |
| 1 | 1 | `0x01` |
| 2–3 | 2 | Plaintext payload length in bytes, big-endian (in practice byte 2 is `0x00`, byte 3 is the length) |
| 4… | 8×k | Payload: the application-layer message, zero-padded to a multiple of 8 bytes, then each 8-byte block XXTEA-encrypted |

**Scale → client:** same layout with high nibble `0x5` in byte 0 and `0x01`
in byte 1. To decode: decrypt each 8-byte block starting at offset 4, then
truncate the concatenated plaintext to the length given at offset 3
(discarding padding). Frame length is always `4 + 8×k`.

---

## 4. Application layer

These messages are the plaintext contents of encrypted frames.

### 4.1 Request format (client → scale)

| Offset | Size | Value |
|---|---|---|
| 0 | 1 | `0x16` |
| 1 | 1 | `0x00` |
| 2–3 | 2 | Length, uint16 LE = number of bytes that follow this field (command byte + flag byte + arguments = argument length + 2) |
| 4 | 1 | Command ID (see §5) |
| 5 | 1 | `0xA8` |
| 6… | var | Command arguments |

### 4.2 Reply / notification format (scale → client)

| Offset | Size | Value |
|---|---|---|
| 0 | 1 | `0x22` |
| 1 | 1 | `0x01` |
| 2–3 | 2 | Length, uint16 LE = number of bytes following this field |
| 4 | 1 | Command ID this message relates to |
| 5 | 1 | `0xA8` |
| 6 | 1 | Status byte (see below) |
| 7… | var | Payload |

**Status byte semantics are inconsistent between commands:**

- For simple acknowledgements (commands `0x01`, `0x03`, `0x04`, `0x05`,
  `0x06`, `0x0A`, `0x0B`, `0x0E`): **0 = success**, non-zero = error. These
  replies are exactly 7 bytes (length field = 3).
- For data-bearing messages (`0x0D` user list, `0x09` historical weight):
  **1 = success/valid**.
- The live weight message (`0x08`) has **no status byte**; its data starts
  at offset 6.

Replies are matched to requests by the command ID at offset 4. Unsolicited
messages (live weight, historical weight) can arrive at any time, so a
client should demultiplex by command ID rather than assuming the next
notification answers the last request.

---

## 5. Commands

Known command IDs (names from the vendor's terminology):

| ID | Name | Implemented/verified |
|---|---|---|
| `0x01` | SYNC_TIME | yes |
| `0x02` | USER_LIST (legacy) | no |
| `0x03` | CURRENT_USER (legacy) | yes |
| `0x04` | SET_UNIT | yes |
| `0x05` | SET_HELLO (greeting) | yes |
| `0x06` | RESET | yes |
| `0x07` | BROAD_TIME | no |
| `0x08` | CUR_WEIGHT_DATA (live weight) | yes (scale → client) |
| `0x09` | HISTORY_WEIGHT_DATA | yes |
| `0x0A` | UPDATE_USER | yes |
| `0x0B` | DEL_USER | yes |
| `0x0C` | DEV_BIND_STATE | no |
| `0x0D` | USER_LIST_NEW | yes |
| `0x0E` | CURRENT_USER_NEW | yes |
| `0x0F` | DEL_ALL_USER | no |
| `0x10` | HEART_MODE | decoded from app (§5.13) |
| `0x11` | HEART_RESULT | decoded from app (§5.13) |
| `0x12` | WEIGHT_MODE | decoded from app (§5.13) |

Commands marked "no" exist in the vendor protocol but their payloads have
not been reverse-engineered.

### 5.13 Heart rate: HEART_MODE (`0x10`), HEART_RESULT (`0x11`), WEIGHT_MODE (`0x12`)

Decoded from the official Wyze/Hualai app (`com.wyze.pluto`,
`ICWyzeProtocol`/`WplHeartRateHomeActivity`). The app measures heart rate
over BLE while you stand on the scale barefoot:

1. **HEART_MODE (`0x10`)** — **no arguments** (length field `0x0002`).
   Puts the scale into heart-rate measurement mode. The scale replies with
   a standard acknowledgement; while its status byte is non-zero the app
   **re-sends HEART_MODE** (roughly every couple of seconds) to keep the
   mode active.

2. **HEART_RESULT (`0x11`)** — unsolicited, streamed while measuring. After
   the `cmd`/`0xA8` header the payload is three bytes:

   | Offset | Size | Field |
   |---|---|---|
   | 6 | 1 | on-scale/status flag (1 = someone is on the scale) |
   | 7 | 1 | `measure_state` — **1 = measurement complete** |
   | 8 | 1 | `heart_rate` — beats per minute |

   The reading is final when `measure_state == 1` and `heart_rate > 0`.

3. **WEIGHT_MODE (`0x12`)** — **no arguments** (length field `0x0002`).
   Returns the scale to normal weighing mode; the app sends it when leaving
   the heart-rate screen.

The app also has a separate phone-camera (PPG) heart-rate path, so a given
model may use either. This BLE flow is transcribed from the app; an earlier
probe failed only because it wrongly appended a 1-byte argument to `0x10`.

**Partial hardware confirmation (WL_SC3):** sending the correct no-argument
`HEART_MODE` does switch the scale out of weight mode (the `0x08` weight
stream stops), so the command is accepted, but no `0x11` result was
observed in testing. The app precedes measurement by selecting a current
user (`0x0E`) and drives an interactive on-screen "stand still" session;
the exact preconditions for the scale to emit `0x11` (current-user
context, a settled weight, sustained stillness) have not been fully
reproduced.

### 5.1 User record (25 bytes)

Used by several commands. Layout:

| Offset | Size | Type | Field |
|---|---|---|---|
| 0 | 16 | bytes | `user_id` — opaque 16-byte identifier, chosen by the client (the official app uses its own account/user IDs; any unique value works) |
| 16 | 2 | uint16 LE | `weight` — kilograms × 100 |
| 18 | 1 | uint8 | `sex` — 1 = male, 0 = female |
| 19 | 1 | uint8 | `age` — years |
| 20 | 1 | uint8 | `height` — centimeters |
| 21 | 1 | uint8 | `athlete_mode` — 1 = on, 0 = off |
| 22 | 1 | uint8 | `only_weight` — 1 = measure weight only (skip impedance/body composition), 0 = full measurement |
| 23 | 2 | uint16 LE | `last_impedance` — most recent impedance reading, 0 if none |

### 5.2 SYNC_TIME (`0x01`)

Sets the scale's clock. The reference client always sends this as the first
command after the key exchange.

- **Arguments (5 bytes):** uint32 LE **standard Unix timestamp (UTC
  seconds)**, followed by one byte `0x01` (purpose unknown; always send
  `0x01`). The official app sends `System.currentTimeMillis() / 1000` with
  no timezone offset, so this is plain UTC epoch time (an earlier revision
  of this document incorrectly described it as local wall time). History
  record timestamps (§5.8) are likewise plain UTC epoch seconds.
- Length field: `0x0007`.
- **Reply:** 7-byte acknowledgement, status 0 = success.

### 5.3 CURRENT_USER (`0x03`, legacy)

Selects the active user using an older 23-byte record: same as §5.1 but
**without** the trailing `last_impedance` field. Length field `0x001B` (the
reference implementation sends the same length value as the new variant).
Reply: acknowledgement, 0 = success. Prefer `0x0E`.

### 5.4 SET_UNIT (`0x04`)

- **Argument (1 byte):** 1 = pounds, 0 = kilograms (display unit only; the
  protocol always reports weight in kg × 100).
- Length field: `0x0003`.
- **Reply:** acknowledgement, 0 = success.

### 5.5 SET_HELLO (`0x05`)

Controls whether the scale shows a greeting when the screen turns on.

- **Argument (1 byte):** 1 = show greeting, 0 = don't.
- Length field: `0x0003`.
- **Reply:** acknowledgement, 0 = success.

### 5.6 RESET (`0x06`)

Factory-resets the scale. **No arguments** (length field `0x0002`).
**Reply:** acknowledgement, 0 = success.

### 5.7 CUR_WEIGHT_DATA (`0x08`) — unsolicited live weight

Sent continuously by the scale while someone is standing on it. Total
message length 51 bytes; length field = 47. No status byte — the payload
starts at offset 6:

| Offset | Size | Type | Field |
|---|---|---|---|
| 6 | 1 | uint8 | Battery level |
| 7 | 1 | uint8 | Current display unit |
| 8 | 16 | bytes | `user_id` of the matched/current user |
| 24 | 1 | uint8 | `sex` (1 = M, 0 = F) |
| 25 | 1 | uint8 | `age` |
| 26 | 1 | uint8 | `height` (cm) |
| 27 | 1 | uint8 | `athlete_mode` |
| 28 | 1 | uint8 | `only_weight` |
| 29 | 1 | uint8 | `measure_state` — **2 = measurement settled/final**; other values indicate an in-progress reading |
| 30 | 2 | uint16 LE | `weight` — kg × 100 |
| 32 | 2 | uint16 LE | `impedance` (ohms) |
| 34 | 2 | uint16 LE | `bfp` — body fat percentage |
| 36 | 2 | uint16 LE | `muscle_mass` |
| 38 | 1 | uint8 | `bone_mass` |
| 39 | 2 | uint16 LE | `water` |
| 41 | 2 | uint16 LE | `protein` |
| 43 | 2 | uint16 LE | `lbm` — lean body mass |
| 45 | 1 | uint8 | `vfal` — visceral fat level |
| 46 | 2 | uint16 LE | `bmr` — basal metabolic rate |
| 48 | 1 | uint8 | `body_age` |
| 49 | 2 | uint16 LE | `bmi` |

Body-composition field scaling (verified against a real measurement):
weight is kg × 100; `bfp`, `water` and `protein` are percent × 10;
`muscle_mass`, `lbm` and `bone_mass` are kg × 10; `bmi` is value × 10;
`impedance` (ohms), `vfal`, `bmr` (kcal) and `body_age` (years) are
unscaled. The verifying frame satisfied `lbm = weight × (1 − bfp)` and
`muscle_mass = lbm − bone_mass` exactly. Fields are zero when
`only_weight` is set or impedance could not be measured.

While a measurement is in progress (`measure_state` 0, then 1 once the
weight stabilizes), `user_id` is all zeros and the composition fields are
empty; the final frame (`measure_state` 2) carries the matched user's ID
and the full body composition.

### 5.8 HISTORY_WEIGHT_DATA (`0x09`) — stored measurements

The scale caches measurements taken while no client was connected. After the
client selects a user with CURRENT_USER_NEW (`0x0E`), the scale spontaneously
sends that user's oldest pending record.

**Record message (scale → client):** 53 bytes total; length field = 49;
status byte at offset 6 (1 = valid record). Payload from offset 7:

| Offset | Size | Type | Field |
|---|---|---|---|
| 7 | 4 | uint32 LE | Unix timestamp of the measurement |
| 11 | 16 | bytes | `user_id` |
| 27 | 1 | uint8 | `sex` (1 = M, 0 = F) |
| 28 | 1 | uint8 | `age` |
| 29 | 1 | uint8 | `height` (cm) |
| 30 | 1 | uint8 | `athlete_mode` |
| 31 | 1 | uint8 | `only_weight` |
| 32 | 2 | uint16 LE | `weight` — kg × 100 |
| 34 | 2 | uint16 LE | `impedance` |
| 36 | 2 | uint16 LE | `bfp` |
| 38 | 2 | uint16 LE | `muscle_mass` |
| 40 | 1 | uint8 | `bone_mass` |
| 41 | 2 | uint16 LE | `water` |
| 43 | 2 | uint16 LE | `protein` |
| 45 | 2 | uint16 LE | `lbm` |
| 47 | 1 | uint8 | `vfal` |
| 48 | 2 | uint16 LE | `bmr` |
| 50 | 1 | uint8 | `body_age` |
| 51 | 2 | uint16 LE | `bmi` |

**Acknowledgement (client → scale):** a request with command `0x09` and a
single argument byte `0x00` (length field `0x0003`). Sending this **deletes
the record from the scale's cache**, after which the scale sends the next
pending record for the selected user (if any). There is no known way to read
subsequent records without acknowledging (and thus deleting) earlier ones.
There is no reply to the acknowledgement itself; the client should treat the
stream as finished when no further record arrives within a timeout (~2 s).

### 5.9 UPDATE_USER (`0x0A`)

Creates or updates a stored user profile.

- **Arguments:** 25-byte user record (§5.1). Length field `0x001B`.
- **Reply:** acknowledgement, 0 = success.

To create a new user, the reference client first sends CURRENT_USER_NEW
(`0x0E`) with the record, then UPDATE_USER (`0x0A`) with the same record.

### 5.10 DEL_USER (`0x0B`)

- **Arguments:** 16-byte `user_id`. Length field `0x0012`.
- **Reply:** acknowledgement, 0 = success.

### 5.11 USER_LIST_NEW (`0x0D`)

Requests the list of stored users. **No arguments** (length field `0x0002`).

**Reply:** the byte at offset 6 is the **number of user records** (observed:
2 with two stored users; earlier believed to be a 1 = success flag), followed
by that many consecutive **25-byte user records** (§5.1). Total message
length is `7 + 25 × N`; the length field is `3 + 25 × N`. The list may be
split across **multiple reply messages**; collect replies until none arrives
within a timeout (~2 s).

### 5.12 CURRENT_USER_NEW (`0x0E`)

Selects the active user — the profile the scale will use for body-composition
math and for matching measurements.

- **Arguments:** 25-byte user record (§5.1). Length field `0x001B`.
- **Reply:** acknowledgement, 0 = success.
- **Side effect:** the scale then begins delivering that user's pending
  HISTORY_WEIGHT_DATA records (§5.8).

---

## 6. Session flow

A typical session:

1. Scan for service `0000FD7B`; connect to the device.
2. Discover the service/characteristic; **subscribe to notifications**.
3. Perform the **key exchange** (§3.1) and derive the XXTEA key (§3.2).
4. Send **SYNC_TIME** (§5.2) — the reference client always does this first.
5. Issue commands as needed, all inside encrypted frames:
   - List users (`0x0D`).
   - For each user of interest: select the user (`0x0E`), then receive and
     acknowledge historical records (`0x09`) until the stream runs dry.
   - Or simply stay connected and consume live `0x08` messages.
6. Disconnect. The encryption key and frame counter are per-connection;
   reconnecting requires a fresh key exchange.

## 7. Implementation notes

- The scale sends unsolicited messages (`0x08`, `0x09`) interleaved with
  command replies; always dispatch on the command ID at offset 4 of the
  decrypted payload.
- Decrypted plaintext may contain trailing zero padding; always truncate to
  the length declared in the outer frame (offset 3) before parsing.
- A decrypted message whose command ID is unrecognized should be ignored,
  not treated as an error.
- The reference implementation uses a 2-second timeout for expected replies
  and for detecting the end of multi-message streams (user list, history).
- The 32-bit Diffie-Hellman exchange provides negligible real security
  (trivially brute-forceable); it is obfuscation, not protection.


## Experimental WL_SCU (Scale Ultra) differences

Observed on one Ultra; these findings do not establish compatibility with
every firmware version.

- FD7B / 0001 advertises Write Without Response and Indicate. Writes must use
  the Ultra client's no-response mode.
- The encrypted request body uses a four-byte inner header:
  `[0x20 | counter, 0x01, body_length, sum(body) & 0xff]`, followed by
  the command, flag and arguments. The outer encryption frame is retained.
- The observed 0x18 reply carries a count followed by 48-byte profile records.
- Live 0x08 readings use kg × 100. Completed readings were observed with
  states 2, 3 and 4; unfinished states are not published.
- The live profile identifier is unsuitable for person assignment: primary
  and all-zero identifiers occurred even when the display named another user.
  Weight matching is explicitly an inference in Home Assistant.
- Composition fields and history commands remain unvalidated.
