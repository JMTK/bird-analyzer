from pathlib import Path
import warnings
import requests

import birdnet.audio_based_prediction
import birdnet.utils
import urllib
from bird import Bird, Response
import json
warnings.filterwarnings("ignore")
import absl.logging
absl.logging.set_verbosity(absl.logging.ERROR)
# create birdnet instance for v2.4

import birdnet
import sounddevice as sd
import soundfile as sf

samplerate = 48000  # Hertz
duration = 3  # seconds
filename = 'output.wav'
api_key="8071c1f1-351c-438d-8650-1d47bd95442c"

species_in_area = birdnet.predict_species_at_location_and_time(35.055851, -80.684868)
print("Found " + str(len(species_in_area)) + " species in your area")

while True:
  try:
    print("Recording for " + str(duration) + " seconds")
    mydata = sd.rec(int(samplerate * duration), samplerate=samplerate,
                    channels=1, blocking=True)
    print("Finished recording")
    sf.write(filename, mydata, samplerate)

    print("Getting predictions...")
    # predict species within the whole audio file
    predictions = birdnet.SpeciesPredictions(birdnet.predict_species_within_audio_file(
      Path(filename),
      species_filter=set(species_in_area.keys()),
      min_confidence=0.5,
      silent=True
    ))

    # get most probable prediction at time interval 0s-3s

    prediction_list = list(predictions[(0.0, duration)].items())

    if (len(prediction_list) != 0):
      prediction, confidence = prediction_list[0]
      scientific_name, regular_name = prediction.split("_")
      print(f"Predicted '{regular_name}' with a confidence of {confidence:.2f}")
      encodedName = urllib.parse.quote(regular_name, safe='/', encoding=None, errors=None)
      url = f"https://nuthatch.lastelm.software/v2/birds?page=1&pageSize=25&name={regular_name}&operator=AND"
      print(url)
      r=requests.get(url, headers={"API-Key":api_key})
      jsonResponse = r.json()
      print(jsonResponse)
      response = Response.from_dict(jsonResponse)
      print(response)
      if (len(response.entities) != 0):
        print(response.entities[0])
      else:
        print("Couldn't find bird in API")
    else: 
      print("No prediction found")

  except KeyboardInterrupt:
    break

  except Exception as e:
    print(e)
