# Third-Party Licenses & Corresponding Source Notice

## 1. Project Own Source Code (MIT License)

The source code authored in this repository (`config.py`, `isolated_switcher.py`, `modern_gui.py`, `stage_release.py`, `build.bat`, and documentation) is licensed under the **MIT License** (see [LICENSE](./LICENSE)), preserving upstream attribution to [54006/cnbxqhdl](https://github.com/54006/cnbxqhdl).

---

## 2. Combined Binary Distribution Notice (GNU GPL v3.0)

Prebuilt Windows executables (`bnet-session-switcher.exe`) bundle third-party open-source libraries, including **PyQt5** (licensed under **GNU GPL v3.0**). Accordingly, the combined binary release package is distributed under the terms of the **GNU General Public License v3.0** (full text provided in [COPYING.GPLv3](./COPYING.GPLv3)).

| Component | Locked Version | License | Official Release / Source Location |
| :--- | :--- | :--- | :--- |
| **PyQt5** | `5.15.11` | GNU GPL v3.0 | https://pypi.org/project/PyQt5/5.15.11/#files |
| **PyQt5-Qt5 (Qt 5 Runtime)** | `5.15.2` | GNU LGPL v3.0 / GPL v3.0 | https://download.qt.io/archive/qt/5.15/5.15.2/single/ |
| **PyQt5-sip** | `12.18.0` | BSD-2-Clause | https://pypi.org/project/PyQt5-sip/12.18.0/#files |
| **psutil** | `7.2.2` | BSD-3-Clause | https://pypi.org/project/psutil/7.2.2/#files |
| **Python Runtime** | `3.12.x` (see `BUILD_INFO.txt`) | PSF License v2 (full text in [LICENSE.Python.txt](./LICENSE.Python.txt)) | https://www.python.org/downloads/source/ |

### Complete Corresponding Source Code
1. **Project Source & Exact Build Scripts**: Every binary release archive includes `corresponding-source.zip` (containing `config.py`, `isolated_switcher.py`, `modern_gui.py`, `stage_release.py`, `build.bat`, `requirements.txt`, `requirements-build.lock.txt`, `LICENSE`, `COPYING.GPLv3`, `LICENSE.Python.txt`, and `THIRD_PARTY_NOTICES.md`) as well as `BUILD_INFO.txt` recording the exact Python interpreter and frozen package versions used to produce the binary.
2. **Third-Party Dependency Sources** (per GPLv3 Section 6(d)):
   - `PyQt5 5.15.11` sdist (`PyQt5-5.15.11.tar.gz`), `PyQt5-sip 12.18.0` sdist (`pyqt5_sip-12.18.0.tar.gz`), and `psutil 7.2.2` sdist (`psutil-7.2.2.tar.gz`): available at their respective PyPI version URLs above.
   - `Qt 5.15.2` corresponding source (`qt-everywhere-src-5.15.2.zip` / `.tar.xz`): available from The Qt Company official archive at `https://download.qt.io/archive/qt/5.15/5.15.2/single/` (note: the `PyQt5-Qt5==5.15.2` PyPI package distributes prebuilt LGPLv3 Qt 5.15.2 shared libraries as a wheel without a separate PyPI sdist).

---

## 3. Permissive Component Copyright & License Texts

### PyQt5-sip (BSD 2-Clause License)

```text
Copyright (c) 2025 Phil Thompson <phil@riverbankcomputing.com>

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
this list of conditions and the following disclaimer in the documentation
and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### psutil (BSD 3-Clause License)

```text
BSD 3-Clause License

Copyright (c) 2009, Jay Loden, Dave Daeschler, Giampaolo Rodola
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

 * Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

 * Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

 * Neither the name of the psutil authors nor the names of its contributors
   may be used to endorse or promote products derived from this software without
   specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
