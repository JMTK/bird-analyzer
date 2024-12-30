Homemade Bird Buddy
===================

Introduction
------------

This is a Python script designed for real-time bird audio detection and analysis. It uses audio recording, species prediction, and data storage to create an automated system for identifying and logging bird species in a specific area.

[Github](https://github.com/JMTK/bird-analyzer)
-----------------------------------------------

Key Components and Functionality
--------------------------------

### 1\. Imports and Initial Setup

The script begins by importing necessary libraries and modules, including:

*   Standard libraries: datetime, os, pathlib, random, time
*   Third-party libraries: requests, elasticsearch, birdnet, sounddevice, soundfile
*   Custom modules: Bird, Response (from bird.py), and configuration settings (from config.py)

### 2\. Elasticsearch Configuration

The script sets up a connection to Elasticsearch for data storage:

`es = elasticsearch.Elasticsearch(elasticsearch_host, ca_certs=cert_loc, http_auth=(elasticsearch_user, elasticsearch_password), max_retries=0, retry_on_timeout=False)`

![](https://jmtk.co/imgupload/images/97ea804bb812bf7.png)

Putting this into Elasticsearch allows me to keep track of birds over time, as well as any other metadata analysis I may want to perform

### 3\. Audio Recording Setup

It configures audio recording parameters:

*   Sample rate: 48000 Hz
*   Recording duration: 3 seconds
*   Input device: 'Logi C615 HD WebCam, MME', stuck right outside my window

### 4\. Species Prediction

The script uses BirdNET to predict species in the area:  
`species_in_area = birdnet.predict_species_at_location_and_time(35.055851, -80.684868)`  
This uses TensorFlow to compare against existing models with your recorded audio. I found this to be quite accurate and fast. Since this was also setup to copy out matching audio, I was able to go behind it and double check if I could really hear it.

### 5\. Discord Notification Function

This was more optional on my end to get real-time notifications. A simple request function `send_discord_notification` is defined to send bird information to a Discord webhook when a high-confidence prediction is made.

![](https://jmtk.co/imgupload/images/ffa1d0ee0bcf180.png)

Discord Webhook Integration

### 6\. Main Loop

The core functionality is in a continuous loop that performs the following steps:

1.  Record audio
2.  Predict bird species from the audio
3.  Fetch detailed information about predicted species from an API
4.  Store the information in Elasticsearch
5.  Send a Discord notification for high-confidence predictions
6.  Archive audio files for significant detections

![](https://jmtk.co/imgupload/images/f49bc5937baffa1.png)

Birds showing up over time with a confidence > 0.1

Key Features
------------

*   Real-time audio recording and analysis
*   Integration with BirdNET for species prediction
*   Data enrichment using an external API
*   Elasticsearch storage for long-term data analysis
*   Discord notifications for significant detections
*   Audio archiving for high-confidence predictions

What's Next?
------------

*   Offload audio prediction into a separate script so that we can continuously record and analyze in 3 second increments
*   Setup the webcam outside of our window feeder instead to take videos or pictures on movement or prediction match
*   Re-train models using output files for even more accurate results
