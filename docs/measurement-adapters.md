# Replay an instrument recording

An approved modular bench station or custom assembly can use the same recorded measurement. Each reading names a component in that project, a command and its position in the operating sequence. The resulting plan and its offline replay kit identify the project, revision and adapter version.

## Use the software workspace

1. Open **Software & protocols** for the approved design. In **Program brief**, describe the instrument, measurement and when to take it. For a barcode station, try “After scanning the printed coupon, measure its mark from a recorded grayscale image. Keep the final visual check.” For a custom panel stand, try “After placing the panel, read its left and right regions using the same camera and reference exposure.”
2. Open **Operating sequence**. Review the component, command, units and prerequisites under **Device bindings and prerequisites**. A supported reading is labelled **Recorded input**. A missing command, such as `set_exposure`, lists the integration work required for that device.
3. Under **Replay a recording**, download the template. Enter the item identifier and fill each operation's input. For an operator step, enter the recorded observation in `note`. Where the template includes an `outcome`, enter one of the outcomes in the plan. A failed inspection stops the replay at that step.
4. Choose the completed JSON as the **Recording file** to view the readings. Save the project to preserve the configuration. Keep the original frames and completed recording separately. A change to the project revision requires a new template.

**Offline replay kit** contains the same plan, template and reader code as the application. From the extracted directory, use Python 3.12 with the versions in `requirements.txt` and run the following command.

```sh
python -m app.measurement_replay operation-plan.json recording.json
```

The CLI writes the result as JSON. Exit code `0` means the replay completed, `2` means the input or plan is invalid, and `3` means a required capability was unavailable or an operator recorded a failed check. An unavailable capability blocks the replay before it produces any observations. The replay reads supplied data and opens no devices.

## Inputs and units

| Adapter and command | Recorded input | Result and check |
| --- | --- | --- |
| `grayscale-camera.relative_signal` | One base64-encoded PNG, a calibration reference ID and three rectangular regions | Relative pixel signal in percent. The reference mean must exceed the dark mean. |
| `barcode-scanner.read_barcode` | One scanner frame in `frame` | Decoded identifier. It must match the recording's item identifier. |
| `serial-balance.weigh` | One supported serial balance frame in `frame` | Stable mass converted to grams. Check tare, range and calibration when collecting the source frame. |

For the optical reading, use one opaque, single-frame, 8-bit grayscale PNG. In `sample`, `dark` and `reference`, supply integer `x`, `y`, `width` and `height` in pixels measured from the upper left. Each rectangle must lie inside the frame and the three must be disjoint. Identify the dark and bright references in `calibration_reference` and use regions from the same exposure. Each side is limited to 4,096 pixels, the area to 4,194,304 pixels and the base64 field to 2,000,000 characters. A recording can contain up to 8 MiB.

The adapter computes `100 × (sample mean − dark mean) / (reference mean − dark mean)`. With means of 70, 20 and 220 digital counts respectively, the reading is 25%. The result preserves the original region means, coordinates, reference ID, frame dimensions and SHA-256. Values outside the span are retained and flagged. Illuminance in lux or calibrated reflectance requires a characterized instrument and calibration against the relevant physical quantity. [Pillow image modes](https://pillow.readthedocs.io/en/stable/handbook/concepts.html#modes) and [region statistics](https://pillow.readthedocs.io/en/stable/reference/ImageStat.html#PIL.ImageStat.Stat.mean).

## Register and compose a command

Define a strict input model, a read function and an entry in the shipped `ADAPTERS` registry. A command declares its unit, input model, blank template and calibration instructions. Its read function takes the validated input and item identifier, returns a dictionary with a JSON-compatible `value` and any source evidence, and raises an error for unusable inputs. The adapter declares its stable ID, integer version, supported commands and integration requirements. An optional station module constrains the component binding. An optional native driver name lets an existing operation use the same parser. Increment the adapter version when its input contract or interpretation changes. The exported kit includes this registry module. [Registry and implementations](../app/measurement_adapters.py).

The chat tool `inspect_project_state` with `include_components=true` supplies the catalog, component instance IDs and native operation IDs. `configure_measurement_steps` accepts the complete list of readings, up to 16. Each entry supplies `key`, `label`, `component_id`, `after_operation`, `adapter_id` and `command`. Readings sharing an anchor retain their supplied order. Submit an empty list to remove the readings. Save/Open preserves the bindings. A change to the bound hardware, underlying operation sequence or installed adapter version requires review and rebinding.

The web endpoints are scoped to the project and take the displayed `revision`. Download a template at `GET /api/projects/{project_id}/software/recording-template`, download the kit at `GET /api/projects/{project_id}/software/replay-kit.zip`, or submit a completed recording to `POST /api/projects/{project_id}/software/replay`. Replay checks the project identity, revision and operation-plan digest. A stale recording returns HTTP 409. A well-formed request for an unavailable command returns a blocked result with its component, command and integration requirements.
