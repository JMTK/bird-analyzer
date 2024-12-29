import datetime
import os
from pathlib import Path
import random
from time import sleep
import warnings
import requests
import shutil

import birdnet.audio_based_prediction
import birdnet.utils
import urllib
from bird import Bird, Response
import json
warnings.filterwarnings("ignore")
import absl.logging
absl.logging.set_verbosity(absl.logging.ERROR)
# create birdnet instance for v2.4
from config import *

import elasticsearch
print("Initializing elasticsearch...")

es = elasticsearch.Elasticsearch(elasticsearch_host, ca_certs=cert_loc,
                http_auth=(elasticsearch_user, elasticsearch_password))

es.indices.create(index="bird-analyzer")
import birdnet
import sounddevice as sd
import soundfile as sf
print("Initializing birdnet...")
samplerate = 48000  # Hertz
duration = 3  # seconds
filename = 'output.wav'
os.makedirs("archives", exist_ok=True)

species_in_area = birdnet.predict_species_at_location_and_time(35.055851, -80.684868)
print("Found " + str(len(species_in_area)) + " species in your area")

def send_discord_notification(bird: Bird, webhook_url: str):
    embed = {
        "title": bird.name,
        "color": 3447003,  # Blue color
        "fields": [
            {"name": "Length", "value": f"{bird.lengthMin} - {bird.lengthMax} cm", "inline": True},
            {"name": "Wingspan", "value": f"{bird.wingspanMin} - {bird.wingspanMax} cm", "inline": True},
            {"name": "Scientific Name", "value": bird.sciName, "inline": False},
            {"name": "Family", "value": bird.family, "inline": False},
            {"name": "Order", "value": bird.order, "inline": False},
            {"name": "Conservation Status", "value": bird.status, "inline": False},
            {"name": "Regions", "value": ", ".join(bird.region), "inline": False}
        ]
    }

    if (bird.images is not None and len(bird.images) != 0):
      image = random.choice(bird.images)
      embed["image"] = {"url": image}

    data = {
        "embeds": [embed]
    }

    response = requests.post(webhook_url, json=data)
    if response.status_code != 204:
        print(f"Failed to send notification: {response.status_code} - {response.text}")

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
      min_confidence=0.4,
      silent=True
    ))

    # get most probable prediction at time interval 0s-3s
    prediction_list = list(predictions[(0.0, duration)].items())

    for prediction in prediction_list:
      names, confidence = prediction
      scientific_name, regular_name = names.split("_")
      print(f"Predicted '{regular_name}' with a confidence of {confidence:.2f}")
      encodedName = urllib.parse.quote(regular_name, safe='/', encoding=None, errors=None)
      url = f"https://nuthatch.lastelm.software/v2/birds?page=1&pageSize=25&name={regular_name}&sciName={scientific_name}&operator=OR"
      r=requests.get(url, headers={"API-Key":api_key})
      responseDict = r.json()
      response = Response.from_dict(responseDict)
      if (len(response.entities) != 0):
        bird = response.entities[0]
        print(bird)
        timestamp = datetime.datetime.now()
        dict = bird.__dict__
        dict["timestamp"] = timestamp
        es.index(index="bird-analyzer",
                    document=dict
        )
        shutil.copy(filename, f"archives/{scientific_name}_{regular_name}_{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}.wav")
        webhook_url = "https://discord.com/api/webhooks/1322685037019402340/mUltuxrIq0MaxQbrG4fTNu4luvpfvU-64YuD3lUjsAWH5SCM5mh-GWE8eVhVliLBm1d1"
        #send_discord_notification(bird, webhook_url)
      else:
        print("Couldn't find bird in API")

  except KeyboardInterrupt:
    break

  except Exception as e:
    print(e)
    sleep(1)
