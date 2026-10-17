#!/bin/bash
# Hand-written pipeline script with every key gen.py uses -> metal-tt -> read
# back what metal-tt kept (metal-source), to learn the exact spelling.
set -x
W=probe; rm -rf $W; mkdir -p $W
xcrun -sdk iphoneos metal -target air64-apple-ios26.0 -c a16/probe.metal -o $W/probe.air
xcrun -sdk iphoneos metallib $W/probe.air -o $W/probe.metallib
python3 a16/gen.py --probe "$PWD/$W/probe.metallib" > $W/probe.mtlp-json
cat $W/probe.mtlp-json
xcrun -sdk iphoneos metal-tt -target air64-apple-ios26.0 $W/probe.mtlp-json -o $W/out.metalar
xcrun metal-lipo -archs $W/out.metalar
xcrun metal-lipo -thin applegpu_g15p -output $W/g15p.metalar $W/out.metalar
for f in out g15p; do
  rm -rf $W/src-$f
  xcrun metal-source -flatbuffers=json $W/$f.metalar -o $W/src-$f && find $W/src-$f -type f && cat $(find $W/src-$f -name '*.mtlp-json' | head -1)
done
# also try with a key metal-tt cannot know, to see if it complains
sed 's/"pixel_format"/"pixel_formatXX"/' $W/probe.mtlp-json > $W/bad.mtlp-json
xcrun -sdk iphoneos metal-tt -target air64-apple-ios26.0 $W/bad.mtlp-json -o $W/bad.metalar; echo "bad key exit: $?"
