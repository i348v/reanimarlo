# Third-party notices

## hls.js

`viewer/static/hls.min.js` is [hls.js](https://github.com/video-dev/hls.js),
included unmodified as a compiled/minified build.

```
Copyright (c) 2017 Dailymotion (http://www.dailymotion.com)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

## arlo-cam-api (not included, external dependency)

This project builds on protocol reverse-engineering from
[Meatballs1/arlo-cam-api](https://github.com/Meatballs1/arlo-cam-api).
That repository does not carry a license file, so **no code from it is
included in this repository**. You clone it yourself as a separate
dependency; see `docs/ARLO_CAM_API_PATCH.md` for the additions this
project needs on top of it, documented as new code rather than a diff of
their file.

## Optional AI dependencies

`viewer/requirements-ai.txt` installs
[ultralytics](https://github.com/ultralytics/ultralytics) (AGPL-3.0 or
commercial license - check their terms before enabling detection) and
[insightface](https://github.com/deepinsight/insightface) (MIT, model
weights under their own separate non-commercial terms - check before
any commercial use). Both are optional and off by default
(`REANIMARLO_AI_DETECTION`).
