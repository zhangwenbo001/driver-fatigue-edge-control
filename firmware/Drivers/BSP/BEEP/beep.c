#include "./BSP/BEEP/beep.h"

void beep_init(void)
{
    GPIO_InitTypeDef gpio_init_struct;

    BEEP_GPIO_CLK_ENABLE();
    HAL_GPIO_WritePin(BEEP_GPIO_PORT, BEEP_GPIO_PIN, GPIO_PIN_RESET);

    gpio_init_struct.Pin = BEEP_GPIO_PIN;
    gpio_init_struct.Mode = GPIO_MODE_OUTPUT_PP;
    gpio_init_struct.Pull = GPIO_PULLDOWN;
    gpio_init_struct.Speed = GPIO_SPEED_FREQ_LOW;
    HAL_GPIO_Init(BEEP_GPIO_PORT, &gpio_init_struct);

    BEEP_OFF();
}

void beep_off_direct(void)
{
    HAL_GPIO_WritePin(BEEP_GPIO_PORT, BEEP_GPIO_PIN, GPIO_PIN_RESET);
}
