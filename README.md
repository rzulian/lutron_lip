# Lutron LIP
# Lutron Custom Component for Home Assistant

This integration connects to Lutron Systems supporting the Lutron Integration Protocol (LIP).

## Features:
- **Homeworks QS and RadioRA support**
- **Recursive areas**: Use parent areas in naming for easier identification, especially in large installations.
- **QS_IO_INTERFACE support**
- **Phantom keypads** support
- **Seetouch international keypads** and a wide range of other keypads supported
- **CCI support**
- **Button states**: Supports press, release, hold, double tap, and hold-release statuses
- **Entity name customization**: Specify if the entity name should include the Lutron area name and/or the parent area name. This is particularly useful in large deployments where multiple rooms may have devices with the same name (e.g., "Central light"), but you need unique names for each device.
- **DB file caching**: Cache the database file to reduce reload time from the controller, which can be slow for large installations. Reloading is generally unnecessary.
- **Variable support**: Read and set Lutron variables. Variables are not available from the controller database, so you must list the integration IDs for variables in the setup.
- **Travel time-based shades**: Since shades often don't report the correct status (open or closed), this feature allows you to specify the travel time for each shade.
- **Keypad LED control**: Keypad buttons configured as "integration" will be created as light devices (`LutronLedLight`), allowing you to control them.
- **Full button functionality**: Supports press, release, hold, double tap, and hold-release statuses.

## RadioRA Mode:
This integration includes a **"RadioRa mode"**, which can be enabled during configuration. When enabled, the integration behaves similarly to the current "official" integration:
- Button names are derived from engravings
- Every valid button is added as a scene
- LEDs are created as switches instead of lights

## Communication:
This integration uses the **aoilip** code to communicate with the Lutron Controller.
