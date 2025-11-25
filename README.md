# RelateAR

## Repos to install:
- SAM-3D-body: this is for 3D generation


### Overall pipeline structure:
- Take in the product that a user chose

- Use GPT to figure out the following:
    - target object 
    - negative mask objects 

- Use Grounded SAM to get the segmentation of the object of interest

- Impaint the object with the use of nano banana

- Use Nano Banana to impaint other objects into the object of interest 
    - the user can mark the location they would like an 
      object to be placed at
    - Prompt structure: 
        can you give me an image with the <other_object> placed on the mark place by the user for the <main_object> 

- Use the final output to then generate a 3D model with the use of SAM 3D

- Take the guassian splat and in blender output the .glb which
  can then be used in AR


### Side notes
- SAM 3D cannot control its generation 
- Have hunyuan 3D where it generates what is provided 
  with more control


### Extra features


### Required imports
- openai
- python-dotenv
- requests 
- beatifulsoup4
- tldextract
- selenium
- pillow 

set -e

sudo apt update

sudo apt install -y wget curl ca-certificates

sudo apt install -y \
  fonts-liberation \
  libasound2t64 \
  libatk-bridge2.0-0 \
  libgbm1 \
  libgtk-3-0 \
  libnss3 \
  libxss1 \
  libxtst6 \
  libu2f-udev \
  libdrm2 \
  libxdamage1 \
  libxrandr2 \
  libappindicator3-1

wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb

sudo apt install -y ./google-chrome-stable_current_amd64.deb 

rm -f /tmp/google-chrome-stable_current_amd64.deb

google-chrome --version


