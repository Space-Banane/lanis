#!/bin/bash

# Default values
OUTPUT_DIR="artifacts"
SKIP_UPGRADE=false
SKIP_CONFIRM=false

# Determine OS first
OS=$(uname)

# Set build defaults based on OS
if [ "$OS" == "Darwin" ]; then
    BUILD_IOS=true
    BUILD_ANDROID=false
else
    BUILD_IOS=false
    BUILD_ANDROID=true
fi

# Function to display help
show_help() {
    cat << EOF
Usage: build.sh [OPTIONS]

Options:
  --skip-upgrade        Skip running 'flutter pub upgrade --major-versions'
  --output-dir DIR      Set the output directory for build artifacts (default: artifacts)
  --skip-confirm        Skip the confirmation prompt before building
  --android             Build Android APK and AAB files
  --ios                 Build iOS IPA file (macOS only)
  --help                Display this help message

Examples:
  build.sh --skip-upgrade
  build.sh --output-dir ./my-builds
  build.sh --android --skip-confirm
  build.sh --ios
  build.sh --android --ios --skip-upgrade
EOF
    exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-upgrade)
            SKIP_UPGRADE=true
            shift
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --skip-confirm)
            SKIP_CONFIRM=true
            shift
            ;;
        --android)
            BUILD_ANDROID=true
            BUILD_IOS=false
            shift
            ;;
        --ios)
            BUILD_IOS=true
            BUILD_ANDROID=false
            shift
            ;;
        --help)
            show_help
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Validate iOS build on non-Darwin platforms
if [ "$BUILD_IOS" = true ] && [ "$OS" != "Darwin" ]; then
    echo "Error: iOS builds are only supported on macOS (Darwin)"
    exit 1
fi

if [ "$OS" == "Darwin" ]; then
    if [ "$BUILD_IOS" = true ]; then
        echo "Building App Store IPA"
    fi
    if [ "$BUILD_ANDROID" = true ]; then
        echo "Building Android APK and AAB files"
    fi
elif [ "$OS" == "Linux" ]; then
    echo "Building selfsigned APK and AAB files"
else
    echo "Unsupported operating system: $OS"
    exit 1
fi
echo ">>> flutter clean"
flutter clean
echo ">>> pub get"
flutter pub get
if [ "$SKIP_UPGRADE" = false ]; then
    echo ">>> pub upgrade"
    flutter pub upgrade --major-versions
    echo " pub outdated"
    flutter pub outdated
else
    echo ">>> skipping pub upgrade"
fi
echo ""
if [ "$SKIP_CONFIRM" = false ]; then
    read -p "Press enter to continue..."
fi
sleep 1
mkdir -p "$OUTPUT_DIR"

echo ">>> generate localization files"
dart run intl_utils:generate


if [ "$OS" == "Darwin" ]; then

    if [ "$BUILD_IOS" = true ]; then
        echo ">>> build IPA"
        flutter build ipa
        mv build/ios/ipa/Lanis.ipa "$OUTPUT_DIR/Lanis.ipa"
    fi

    if [ "$BUILD_ANDROID" = true ]; then
        echo ">>> build appbundle"
        flutter build appbundle --dart-define=cronetHttpNoPlay=true
        mv build/app/outputs/bundle/release/app-release.aab "$OUTPUT_DIR/app-release.aab"

        echo ">>> build apk"
        flutter build apk --split-per-abi --dart-define=cronetHttpNoPlay=true
        mv build/app/outputs/flutter-apk/app-armeabi-v7a-release.apk "$OUTPUT_DIR/app-armeabi-v7a-release-selfsigned.apk"
        mv build/app/outputs/flutter-apk/app-arm64-v8a-release.apk "$OUTPUT_DIR/app-arm64-v8a-release-selfsigned.apk"
        mv build/app/outputs/flutter-apk/app-x86_64-release.apk "$OUTPUT_DIR/app-x86_64-release-selfsigned.apk"
    fi

    if [ "$BUILD_IOS" = true ] || [ "$BUILD_ANDROID" = true ]; then
        open "$OUTPUT_DIR"
    fi
elif [ "$OS" == "Linux" ]; then
    if [ "$BUILD_ANDROID" = true ]; then
        echo ">>> build appbundle"
        flutter build appbundle --dart-define=cronetHttpNoPlay=true
        mv build/app/outputs/bundle/release/app-release.aab "$OUTPUT_DIR/app-release.aab"

        echo ">>> build apk"
        flutter build apk --split-per-abi --dart-define=cronetHttpNoPlay=true
        mv build/app/outputs/flutter-apk/app-armeabi-v7a-release.apk "$OUTPUT_DIR/app-armeabi-v7a-release-selfsigned.apk"
        mv build/app/outputs/flutter-apk/app-arm64-v8a-release.apk "$OUTPUT_DIR/app-arm64-v8a-release-selfsigned.apk"
        mv build/app/outputs/flutter-apk/app-x86_64-release.apk "$OUTPUT_DIR/app-x86_64-release-selfsigned.apk"

        xdg-open "$OUTPUT_DIR"

        # Kill left over gradle daemons
        pkill -f '.GradleDaemon.'
    fi
fi
echo "done."
