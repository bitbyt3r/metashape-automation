#!/bin/env python3
import asyncio
from autobahn import wamp
from autobahn.asyncio.wamp import ApplicationSession, ApplicationRunner
import Metashape
import socket
import sys
import urllib.request
from xml.dom import minidom
import zipfile
import os
import math
import requests
import traceback
import shutil
import json

class Component(ApplicationSession):
    @asyncio.coroutine
    def onJoin(self, details):
        self.received = 0
        results = yield from self.subscribe(self)
        for res in results:
            if isinstance(res, wamp.protocol.Subscription):
                print("Ok, subscribed handler with subscription ID {}".format(res.id))
            else:
                print("Failed to subscribe handler: {}".format(res))
        self.register_name()
        sequences = []
        for i in range(int(sys.argv[1]), int(sys.argv[2])):
            sequence = yield from self.call('com.scanmanager.query', datatype="SequenceImages", matches={"ID": i})
            sequences.append(sequence[0])
        print(json.dumps(sequences))
        with open("sequences.json", "w") as FILE:
            FILE.write(json.dumps(sequences))
        self.session.leave()

    @wamp.subscribe(u'com.scanmanager.ready')
    def register_name(self):
        print("Registering name")
        yield from self.call(u'com.scanmanager.register_name', name=socket.gethostname()+"-Metashape")

    def onDisconnect(self):
        asyncio.get_event_loop().stop()

def process(sequences):
    print("The following sequences will be processed:")
    for i in sequences:
        print(i['name'])
    for i in sequences:
        try:
            ziploc = "images-{}.zip".format(i['ID'])
            image_path = "./images-{}".format(i['ID'])
            safename = i['name'].replace(" ", "").lower()
            proj_path = "./{}".format(safename)
            if not os.path.isdir(proj_path):
                os.makedirs(proj_path)
            if os.path.isfile(os.path.join(proj_path, "data", "sequence.json")):
                continue
            if not os.path.isdir(image_path):
                if not os.path.isfile(ziploc):
                    print("Fetching {}".format(i['ID']))
                    url = "http://photomaster2.irc.umbc.edu/download/images.zip?sequences={}".format(i['ID'])
                    with open(ziploc, "wb") as FILE:
                        for datachunk in requests.get(url, verify=False, stream=True).iter_content(100000):
                            FILE.write(datachunk)
                print("Unzipping {}".format(i['ID']))
                zipf = zipfile.ZipFile(ziploc, "r")
                os.makedirs(image_path)
                zipf.extractall(image_path)
                zipf.close()
                os.remove(ziploc)

            doc = Metashape.app.document
            chunk = doc.addChunk()
            doc.save(os.path.join(proj_path, "project.psz"), chunks = [chunk,])
            chunk.label = i['name']
            images = [os.path.join(dp, f) for dp, dn, fn in os.walk(image_path) for f in fn]
            if not os.path.isdir(os.path.join(proj_path, "images")):
                os.makedirs(os.path.join(proj_path, "images"))
            for image in images:
                shutil.copy(image, os.path.join(proj_path, "images"))
            print("Adding {} photos...".format(len(images)))
            chunk.addPhotos(images)

            for camera in chunk.cameras:
                sensor = chunk.addSensor()
                sensor.label = camera.label
                for attr in ['type', 'calibration', 'width', 'height', 'focal_length', 'pixel_height', 'pixel_width']:
                    setattr(sensor, attr, getattr(camera.sensor, attr))
                camera.sensor = sensor

            print("Locating marker references...")
            markerpath = "markers.json"
            with open(markerpath, "r") as MARKERFILE:
                markers = json.loads(MARKERFILE.read())
            markerDict = {label:Metashape.Vector(markers[label]) for label in markers.keys()}

            chunk.detectMarkers(Metashape.TargetType.CircularTarget12bit, 50)
            for marker in chunk.markers:
                if marker.label in markerDict:
                    print("Updating target {} to loc {}".format(marker.label, markerDict[marker.label]))
                    marker.reference.location = markerDict[marker.label]
            print("Transforming build region")
            region = chunk.region
            if chunk.transform:
                transform = chunk.transform
            else:
                transform = Metashape.Matrix([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]])
                print("Chunk has transform:", transform)
            rot_temp = transform.matrix * Metashape.Matrix([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]])
                
            s = math.sqrt(rot_temp[0, 0]**2 + rot_temp[0, 1]**2 + rot_temp[0, 2]**2) 
            R = Metashape.Matrix( [[rot_temp[0,0],rot_temp[0,1],rot_temp[0,2]], [rot_temp[1,0],rot_temp[1,1],rot_temp[1,2]], [rot_temp[2,0],rot_temp[2,1],rot_temp[2,2]]])
            R = R * (1.0 / s)

            #<---- Size ---->
            regionSize = [1,2.5,1]
            regionCenter = [0,1.25,0]
            inter_size = Metashape.Vector([0, 0, 0])
            geo_size = Metashape.Vector([regionSize[0], regionSize[1], regionSize[2]])
            inter_size = geo_size / s  

            #<---- Center ---->
            geo_cen = Metashape.Vector([regionCenter[0], regionCenter[1], regionCenter[2]])
            inter_cen = transform.matrix.inv().mulp(geo_cen)

            reg = Metashape.Region()
            reg.rot = R.t() 
            reg.size = inter_size
            reg.center = inter_cen
            chunk.region = reg
            chunk.camera_crs = chunk.crs
            chunk.marker_crs = chunk.crs
            chunk.updateTransform()
            print("Moving chunk region to ", chunk.region.rot, chunk.region.center, chunk.region.size)

            doc.save(os.path.join(proj_path, "project.psz"), chunks = [chunk,])
            print("Importing Masks")
            chunk.importMasks("Mask\\{filename}.cr2", source=Metashape.MaskSourceBackground, tolerance=23, cameras=chunk.cameras)
            print("Matching photos")
            chunk.matchPhotos(accuracy=Metashape.HighAccuracy, filter_mask=True, mask_tiepoints=True, preselection=Metashape.Preselection.NoPreselection)
            doc.save(os.path.join(proj_path, "project.psz"), chunks = [chunk,])
            print("Importing camera locations")
            chunk.importCameras("cameras.xml")
            print("Building Sparse Pointcloud")
            chunk.buildPoints()
            doc.save(os.path.join(proj_path, "project.psz"), chunks = [chunk,])
            
            chunk.buildDepthMaps(quality=Metashape.HighQuality, filter=Metashape.MildFiltering)
            doc.save(os.path.join(proj_path, "project.psz"), chunks = [chunk,])
            chunk.buildModel(source=Metashape.DepthMapsData, surface=Metashape.Arbitrary, interpolation=Metashape.EnabledInterpolation, face_count=Metashape.HighFaceCount, keep_depth=False)
            doc.save(os.path.join(proj_path, "project.psz"), chunks = [chunk,])
            chunk.buildUV(mapping=Metashape.GenericMapping)
            doc.save(os.path.join(proj_path, "project.psz"), chunks = [chunk,])
            chunk.buildTexture(blending=Metashape.MosaicBlending, size=8192)
            doc.save(os.path.join(proj_path, "project.psz"), chunks = [chunk,])
            if not os.path.isdir(os.path.join(proj_path, "model")):
                os.makedirs(os.path.join(proj_path, "model"))
            if not os.path.isdir(os.path.join(proj_path, "data")):
                os.makedirs(os.path.join(proj_path, "data"))
            chunk.exportCameras(os.path.join(proj_path, "data", "cameras.xml"))
            chunk.exportMarkers(os.path.join(proj_path, "data", "markers.xml"))
            chunk.exportModel(os.path.join(proj_path, "model", "{}.obj".format(safename)), format=Metashape.ModelFormatOBJ)
            with open(os.path.join(proj_path, "data", "sequence.json"), "w") as FILE:
                FILE.write(json.dumps(i))
        except KeyboardInterrupt:
            traceback.print_exc()
            print("Stopping due to user input")
            return

        except Exception as e:
            print(e)
            traceback.print_exc()
            print("Failed to run images. Moving on...")
        
if __name__ == '__main__':
    if not os.path.isfile("sequences.json"):
        runner = ApplicationRunner(
            u"ws://photomaster2.irc.umbc.edu:8080/ws",
            u"realm1",
        )
        runner.run(Component)
    with open("./sequences.json", "r") as FILE:
        sequences = json.loads(FILE.read())
    process(sequences)