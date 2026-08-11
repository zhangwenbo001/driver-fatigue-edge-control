# Firmware source scope

This folder contains the project-specific application and driver changes for STM32F103ZET6:

- DMS1 parser and RX/output FreeRTOS tasks;
- stack-overflow safe-stop hook;
- USART1 receive handoff;
- LED and active-buzzer drivers;
- Keil project file for reference.

The original vendor template, STM32 HAL, CMSIS, FreeRTOS distribution, generated objects, and binaries are deliberately not redistributed here. To rebuild, add compatible official STM32F1 HAL/CMSIS and FreeRTOS dependencies to the Keil project, then confirm include paths and target settings for the local toolchain.
