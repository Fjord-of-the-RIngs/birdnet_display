# Changelog

## v1.0.2 - 2026-07-10

- Added attributed Avicommons photo caching for detected BirdNET species.
- Added optimized WebP display assets and safe compatibility routes for responsive photo loading.
- Improved recent-detection performance by using the Date/Time index before timestamp filtering.
- Replaced public filesystem-oriented no-photo states with a friendly fallback image and moved photo management behind admin authentication.
- Made image-cache discovery dynamic and corrected display launcher defaults for systemd services.
