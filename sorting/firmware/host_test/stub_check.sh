#!/bin/sh
# 只做语法检查：用一张假的 main.h 把 main_stm32.c 编一遍。
#
#   sh sorting/firmware/host_test/stub_check.sh
#
# 作用：main_stm32.c 依赖 CubeMX 生成的 main.h 和真实外设句柄，本机没有，
# 所以它从来没被编译器看过一眼。这个脚本用最小桩头把它过一遍语法和类型，
# 能抓住拼写错误、漏声明、类型不匹配这类问题——但**抓不到逻辑错误**，
# 逻辑错误只有上板才知道。
#
# 桩头写到临时目录，不进仓库：仓库里不该出现假的 HAL 定义，
# 免得有人误以为那就是真的。
set -e

HERE=$(cd "$(dirname "$0")/.." && pwd)
STUB=${TMPDIR:-/tmp}/sorting_fw_stub
mkdir -p "$STUB"

cat > "$STUB/main.h" <<'EOF'
#ifndef STUB_MAIN_H
#define STUB_MAIN_H
#include <stdint.h>
#include <stddef.h>
typedef struct { int _; } TIM_HandleTypeDef;
typedef struct { int _; } UART_HandleTypeDef;
typedef struct { int _; } IWDG_HandleTypeDef;
typedef struct { int _; } GPIO_TypeDef;
#define GPIOD ((GPIO_TypeDef*)1)
#define GPIOE ((GPIO_TypeDef*)2)
#define GPIO_PIN_0 1u
#define GPIO_PIN_1 2u
#define GPIO_PIN_2 4u
#define GPIO_PIN_3 8u
#define GPIO_PIN_4 16u
#define GPIO_PIN_5 32u
#define GPIO_PIN_6 64u
#define GPIO_PIN_7 128u
#define GPIO_PIN_SET 1
#define GPIO_PIN_RESET 0
#define TIM_CHANNEL_1 0u
extern TIM_HandleTypeDef htim2, htim3, htim4, htim5;
extern UART_HandleTypeDef huart1, huart3;
extern IWDG_HandleTypeDef hiwdg;
extern uint32_t SystemCoreClock;
void HAL_Init(void);
void SystemClock_Config(void);
void MX_GPIO_Init(void);
void MX_DMA_Init(void);
void MX_TIM2_Init(void);
void MX_TIM3_Init(void);
void MX_TIM4_Init(void);
void MX_TIM5_Init(void);
void MX_USART1_UART_Init(void);
void MX_USART3_UART_Init(void);
void HAL_GPIO_WritePin(GPIO_TypeDef *, uint16_t, int);
int HAL_TIM_OC_Start_IT(TIM_HandleTypeDef *, uint32_t);
int HAL_TIM_OC_Stop_IT(TIM_HandleTypeDef *, uint32_t);
#define __HAL_TIM_SET_AUTORELOAD(h, v) ((void)(h), (void)(v))
int HAL_UART_Transmit_DMA(UART_HandleTypeDef *, uint8_t *, uint16_t);
int HAL_UARTEx_ReceiveToIdle_DMA(UART_HandleTypeDef *, uint8_t *, uint16_t);
int HAL_UART_AbortReceive(UART_HandleTypeDef *);
uint32_t HAL_GetTick(void);
int HAL_IWDG_Refresh(IWDG_HandleTypeDef *);
#endif
EOF

cd "$HERE"
gcc -std=c11 -Wall -Wextra -fsyntax-only -I inc -I "$STUB" src/main_stm32.c
echo "main_stm32.c 语法检查通过（桩头，非真 HAL）"
