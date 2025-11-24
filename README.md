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




### Extra features



