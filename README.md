# Driver Fatigue Edge Control

> Public, privacy-sanitized portfolio edition.

This project connects a Windows vision host to an STM32F103ZET6 controller for driver-fatigue warnings. The host detects eye and mouth states with YOLOv8, converts them into time-based fatigue states, and sends a DMS1 serial heartbeat to the MCU. The MCU uses FreeRTOS to validate frames, detect link loss, and drive LED/buzzer warnings.

```text
USB camera
  -> YOLOv8 detection (closed_eye / closed_mouth / open_eye / open_mouth)
  -> time-based fatigue state machine
  -> DMS1 UART heartbeat (115200 8N1, 2 Hz)
  -> STM32F103ZET6 + FreeRTOS
  -> LED and active-buzzer warning
```

## Highlights

- Separates vision inference, fatigue decision, GUI, serial transport, and MCU control into testable modules.
- Uses a monotonic-clock state machine: closed eyes for 1.5 s trigger `FATIGUE`; open mouth for 1.0 s triggers `YAWN`; missing or stale evidence safely falls back to `UNKNOWN`.
- Uses a bounded latest-frame queue on the host and a length-one latest-state queue on the MCU to avoid processing stale real-time data.
- Implements DMS1 framing, sequence rollover, 2 Hz heartbeat, stale-result fallback, reconnect backoff, partial/sticky/oversize frame handling, and 2 s `LINK_LOST` detection.
- Uses static FreeRTOS tasks, StreamBuffer, and queue objects; the stack-overflow hook silences the buzzer and enters a safe stop state.
- Exports `best.pt` to ONNX opset 12. On the 306-image evaluation set, PT and ONNX both reached mAP50 0.9287 and mAP50-95 0.6759. The measured Windows CPU ONNX Runtime baseline was 16.4 FPS with 64.1 ms P95 latency.

## Repository layout

```text
host_app/       Windows GUI, YOLO wrapper, state machine, and serial publisher
configs/        Runtime calibration (`fatigue.yaml`)
firmware/       Project-specific STM32/FreeRTOS application and driver sources
deployment/     PT-to-ONNX export and evaluation scripts; ONNX deployment model
tests/          State-machine, protocol, and integration tests
tools/          Serial simulator
docs/           DMS1 protocol specification
```

## Run the Windows host

```powershell
python -m pip install -r host_app/requirements.txt
Set-Location host_app
python main.py --port COM3 --baud 115200
```

Use the actual COM port for the board. The GUI starts its serial publisher and inference thread on launch; open the camera from the GUI.

## STM32 firmware

`firmware/` contains the application-specific FreeRTOS tasks, DMS1 parser, UART hook, LED/buzzer drivers, and the Keil project file. Third-party HAL/CMSIS/FreeRTOS template sources and build outputs are intentionally excluded from this public repository. Add the appropriate official STM32F1 HAL, CMSIS, and FreeRTOS dependencies before rebuilding the Keil project.

The DMS1 protocol is documented in [docs/protocol_dms1.md](docs/protocol_dms1.md).

## Verification scope

- 36 fatigue-state unit tests and 56 integration tests passed in the release verification.
- PT/ONNX equivalence was evaluated on a 306-image test set. The source dataset and face images are intentionally not included in this public repository.
- The published model artifacts are included for portfolio and local evaluation use.
- Linux/NPU target-board measurements, vendor-specific conversion, temperature, and power tests have not been performed because no target Linux board is available. They are not claimed as completed.

## Privacy and publishing notes

This public edition excludes resumes, personal contact details, student identifiers, school identifiers, local paths, raw datasets and facial sample images, local agent logs, build artifacts, and internal test evidence. The original working project remains separate and unchanged.
