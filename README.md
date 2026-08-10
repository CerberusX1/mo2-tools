# mo2-tools
usefull tools to help in the creation of modlists for wabbajack

in order for any of my tools to work you must first follow these steps

step 1. open terminal/command prompt as admin

step 2. enter the following command     

pip install requests     - use this one, if it fails use other

py -3 -m pip install requests   - this one if you have multiple versions of python installed

python -m pip install requests   -this one if you dont have multiple versions of python installed








the changelog generator

this is used to automatically generate changelogs for your mo2 modlists used in wabbajack

step 1 download the file and extract it to its own folder

step 2 run run_setup.bat to do your initial set up and configuration of the tool and to generate a base line for your modlist

step 3, run run_changelog.bat any time you have added new mods and wish to generate a new changelog

step 4 enjoy less work


notes

when you first run the set up it will appear to not be working, just let it do its thing it might take a bit, its building a snapshot of your modlist.

to get the correct version number in the changelog please compile your modlist in wabbajack before running the changelog tool, other wise you can edit the version number manually
