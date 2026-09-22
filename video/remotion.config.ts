import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setJpegQuality(92);
// Chromium is already installed; no need to download a headless shell.
Config.setBrowserExecutable('/usr/bin/chromium');
