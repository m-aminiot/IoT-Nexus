# Device online/offline status

Every MQTT device must send its connection status to this topic:

```text
isonline
```

The message must be JSON and must include the device name and its status.

## When the device connects

Immediately after the device connects successfully to the MQTT broker, it must publish this message to `isonline`:

```json
{"name": "esp32-room2", "status": "online"}
```

Replace `esp32-room2` with the real, unique name of the device.

Use `retain = true` when publishing this message. This lets the dashboard receive the latest status even when the dashboard starts later.

## When the device disconnects unexpectedly

Configure an MQTT **Last Will and Testament (LWT)** before connecting to the broker.

The Last Will must use the same topic, `isonline`, and this message:

```json
{"name": "esp32-room2", "status": "offline"}
```

Use `retain = true` for the Last Will too.

If the device loses power, Wi-Fi, or its connection to the broker, the broker will automatically publish the Last Will message. The dashboard will then show that device as offline and save the time of that event as its last online time.

## Required sequence

1. Set the Last Will message to `offline`.
2. Connect to the MQTT broker.
3. As soon as the connection succeeds, publish the `online` message to `isonline`.

## Example values

| Situation | Topic | Message |
| --- | --- | --- |
| Device connected | `isonline` | `{"name": "esp32-room2", "status": "online"}` |
| Device disconnected unexpectedly (Last Will) | `isonline` | `{"name": "esp32-room2", "status": "offline"}` |

Do not send plain text such as `esp32-room2 online`. Send the JSON messages exactly as shown above.
