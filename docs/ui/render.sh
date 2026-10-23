#!/bin/bash
# render.sh page [page...]: desktop light, desktop dark, narrow light PNGs into img/
# H / HN override the desktop / narrow capture heights.
D=$(cd "$(dirname "$0")" && pwd -W)    # D:/... form
C="/c/Program Files/Google/Chrome/Application/chrome.exe"
UD="$(cygpath -m "$LOCALAPPDATA")/Temp/hyperloom-chrome"
shot(){ "$C" --headless=new --disable-gpu --user-data-dir="$UD" --hide-scrollbars --window-size="$1" --screenshot="$D/img/$2.png" "file:///$D/mockups/$3" 2>&1 | grep -q written || echo "FAILED $2"; }
for p in "$@"; do
  shot 1440,${H:-1000} "$p-desktop" "$p.html"
  shot 1440,${H:-1000} "$p-desktop-dark" "$p.html#dark"
  shot 600,${HN:-1700} "$p-narrow" "$p.html#narrow"; python -c "from PIL import Image;im=Image.open('$D/img/$p-narrow.png');im.crop((0,0,390,im.size[1])).save('$D/img/$p-narrow.png')"
  echo "rendered $p"
done
