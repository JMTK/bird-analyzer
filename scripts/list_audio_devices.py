"""List audio devices available through sounddevice."""

import sounddevice as sd


def main() -> None:
  default_input, default_output = sd.default.device
  devices = sd.query_devices()

  if not devices:
    print("No audio devices found.")
    return

  print(f"Default input device: {default_input}")
  print(f"Default output device: {default_output}")
  print()

  for index, device in enumerate(devices):
    input_channels = int(device["max_input_channels"])
    output_channels = int(device["max_output_channels"])
    capabilities = []
    if input_channels:
      capabilities.append(f"input: {input_channels} channel(s)")
    if output_channels:
      capabilities.append(f"output: {output_channels} channel(s)")
    description = ", ".join(capabilities) or "no input/output channels"

    markers = []
    if index == default_input:
      markers.append("default input")
    if index == default_output:
      markers.append("default output")
    marker_text = f" [{', '.join(markers)}]" if markers else ""

    print(f"{index}: {device['name']}{marker_text}")
    print(f"   {description}; default sample rate: {device['default_samplerate']:.0f} Hz")


if __name__ == "__main__":
  main()