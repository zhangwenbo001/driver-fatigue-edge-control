#include "./SYSTEM/sys/sys.h"
#include "./SYSTEM/usart/usart.h"
#include "./SYSTEM/delay/delay.h"
#include "./BSP/LED/led.h"
#include "./BSP/BEEP/beep.h"
#include "freertos_hooks.h"
#include "dms_app.h"
#include "FreeRTOS.h"
#include "task.h"

int main(void)
{
    HAL_Init();
    sys_stm32_clock_init(RCC_PLL_MUL9);
    delay_init(72);
    led_init();
    beep_init();

    if (dms_app_init() == 0) {
        usart_init(115200);
    }

    freertos_hooks_register();

    vTaskStartScheduler();

    while (1) {}
}
