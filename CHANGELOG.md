# 2.0.2
* Allowed scan intervals as low as 1 second so existing configurations can update to the seconds-based scheduler

# 2.0.1
* Fixed builds on Home Assistant Supervisor 2026.04 and newer by using an explicit multi-architecture base image

# 2.0.0
* Added a Home Assistant Ingress status page with a manual Run now control
* Added worker state, last-run result, media counts, search, and status/type filters
* Added cached movie and TV media discovery with audio codec and conversion status reporting
* Changed scan intervals from minutes to seconds, with a default of 300 seconds

# 0.1.7
* Reverted the 0.1.6 changes, restoring the 0.1.5 converter and script-copy behavior

# 0.1.5
* Removed subtitle copy from ffmpeg as mp4 does not support it
* Change lock file path as /tmp is not readable

# 0.1.4
* Added -nostdin to ffmpeg command to prevent logging the "Enter ..." input message

# 0.1.3
* Added lock file to prevent re-running until finished

# 0.1.2
* Added config for movie & tv show folders
* Added a changelog file

# 0.1.1
* Make a defaults script folder to copy the scripts with

# 0.1.0
* Initial release
