#ifndef __BEEP_H
#define __BEEP_H

#include "./SYSTEM/sys/sys.h"

#define BEEP_GPIO_PORT          GPIOB
#define BEEP_GPIO_PIN           GPIO_PIN_8
#define BEEP_GPIO_CLK_ENABLE()  do{ __HAL_RCC_GPIOB_CLK_ENABLE(); }while(0)

#define BEEP_ON()   HAL_GPIO_WritePin(BEEP_GPIO_PORT, BEEP_GPIO_PIN, GPIO_PIN_SET)
#define BEEP_OFF()  HAL_GPIO_WritePin(BEEP_GPIO_PORT, BEEP_GPIO_PIN, GPIO_PIN_RESET)

void beep_init(void);
void beep_off_direct(void);

#endif
