/* STM32F407 + 4×TMC2209 —— hal.h 的实现骨架。
 *
 * ⚠ 这个文件**没有在本机编译过**：没有 ARM 工具链，也没有板子和外设。
 *   它的作用是把"上板要接哪些线、每个回调该干什么"写死成一张清单，
 *   免得装配时对着 CubeMX 从零想。真正要补的是每个函数体里的 TODO，
 *   结构、时序约定和状态机都不用改（machine.c 已经测过了）。
 *
 * 上板顺序（建议）：
 *   1. 只做 PING/PONG —— 打通 UART，证明协议层和上位机对得上
 *   2. 接一根轴，只做 HOME —— 证明限位和脉冲方向是对的
 *   3. 接 GOTO —— 证明 mm -> steps 换算和梯形曲线没算错（拿尺子量）
 *   4. 接 Z 和 GRIP —— 最后再做 StallGuard，它依赖前面的位置都可信
 *
 * 分工再确认：上位机只发"到哪"，插补/加减速/限位/堵转全在这里。
 * 这里崩了上位机没法救，所以每个能卡住的地方都要有超时。
 */

#include "main.h"      /* CubeMX 生成：stm32f4xx_hal.h 和各外设句柄 */

#include <string.h>

#include "hal.h"
#include "machine.h"
#include "motion.h"
#include "proto.h"

/* ============================================================== 全局状态 */

/* 每轴累计步数（带符号）。位置就从它换算，**不要用"目标位置"代替**：
 * 丢步时目标位置还是对的，人就被骗了。 */
static long g_pos_steps[AX_COUNT];

/* UART 收不定长行：DMA 收到这里，IDLE 中断表示一行结束 */
static uint8_t rx_dma_buf[PROTO_LINE_MAX];

static machine_t g_machine;

/* ================================================================== 轴配置 */

typedef struct {
    TIM_HandleTypeDef *htim;      /* 脉冲：TIM 输出比较翻转 */
    GPIO_TypeDef      *dir_port;
    uint16_t           dir_pin;
    GPIO_TypeDef      *en_port;   /* TMC2209 EN，低有效 */
    uint16_t           en_pin;
    GPIO_TypeDef      *limit_port;/* 原点限位，上拉输入 */
    uint16_t           limit_pin;
    float              steps_per_mm;
    float              vmax_mm_s;
    float              accel_mm_s2;
    UART_HandleTypeDef *tmc_uart; /* TMC2209 单线 UART（半双工） */
    uint8_t            tmc_addr;  /* MS1/MS2 决定的从机地址 */
} axis_t;

/* 齿数/模数要不要在这里再写一遍？
 * 不要。换算只在 machine.toml 里做一次，这里直接给算好的 steps_per_mm，
 * 否则两处各算一次，改齿轮时会漏掉一处。 */
static axis_t AX[AX_COUNT] = {
    /* TODO: 按实际接线填。下面是占位，不是最终接线。 */
    { &htim2, GPIOD, GPIO_PIN_0, GPIOD, GPIO_PIN_1, GPIOE, GPIO_PIN_0,
      50.93f, 200.0f, 500.0f, &huart3, 0 },   /* X */
    { &htim3, GPIOD, GPIO_PIN_2, GPIOD, GPIO_PIN_3, GPIOE, GPIO_PIN_1,
      50.93f, 200.0f, 500.0f, &huart3, 1 },   /* Y */
    { &htim4, GPIOD, GPIO_PIN_4, GPIOD, GPIO_PIN_5, GPIOE, GPIO_PIN_2,
      127.3f, 100.0f, 500.0f, &huart3, 2 },   /* Z */
    { &htim5, GPIOD, GPIO_PIN_6, GPIOD, GPIO_PIN_7, GPIOE, GPIO_PIN_3,
      63.66f,  60.0f, 500.0f, &huart3, 3 },   /* GRIP */
};

/* 每个轴的运动状态：剩余步数、当前周期、方向 */
typedef struct {
    long  remaining;
    long  total;
    float vpeak;
    float accel;
    float period_us;      /* 当前脉冲周期 */
    float period_min_us;  /* = 1e6 / (vpeak * steps_per_mm)，匀速段的周期 */
    int   running;
} axrun_t;

static axrun_t RUN[AX_COUNT];

/* ================================================================== 应答 */

/* HAL 层负责补 '\n'。用 DMA，避免在主循环里阻塞。
 * ⚠ 缓冲区不能用局部变量（DMA 还没发完函数就返回了）。用两张轮转，
 *   或者检查上一次的 DMA 完成标志。 */
static char txbuf[2][PROTO_LINE_MAX + 1];
static int  txslot;

static void hal_reply(const char *line)
{
    size_t n = strlen(line);
    char *b = txbuf[txslot];
    txslot ^= 1;

    if (n > PROTO_LINE_MAX) {
        n = PROTO_LINE_MAX;   /* 不该发生，proto.c 已经保证长度 */
    }
    memcpy(b, line, n);
    b[n] = '\n';

    /* TODO: 等上一张缓冲发完（HAL_UART_GetState）再启动，否则会覆盖 */
    HAL_UART_Transmit_DMA(&huart1, (uint8_t *)b, (uint16_t)(n + 1));
}

/* ================================================================== 运动 */

static int hal_begin_move(int axis, long steps, float vpeak, float accel)
{
    axis_t *a = &AX[axis];
    axrun_t *r = &RUN[axis];

    if (axis < 0 || axis >= AX_COUNT || steps == 0) {
        return -1;
    }

    /* 方向先于脉冲一个周期置好，TMC2209 要求 DIR 建立时间 ≥ 20ns，
     * 实际留几百 ns 更保险，别和第一个脉冲抢同一边沿。 */
    HAL_GPIO_WritePin(a->dir_port, a->dir_pin,
                      (steps > 0) ? GPIO_PIN_SET : GPIO_PIN_RESET);

    r->total = (steps > 0) ? steps : -steps;
    r->remaining = r->total;
    r->vpeak = vpeak;
    r->accel = accel;
    r->period_min_us = motion_step_period_us(vpeak, a->steps_per_mm);
    r->running = 1;

    /* TODO: 实现加速段。
     * 做法：进 TIM 更新中断，每次中断产生一个脉冲，并按"当前已走步数"
     * 用运动学反算此刻应有的周期，重装 ARR。公式：
     *     已走距离 s = (total - remaining) / steps_per_mm
     *     v = sqrt(2*a*s)  （加速段），到 vpeak 后保持
     *     ARR = period_us - 1  （72MHz 时基下 ARR = us*72 - 1）
     * 这里是骨架，先把"等速 + 计步"跑通再加速度曲线。 */
    r->period_us = r->period_min_us;
    __HAL_TIM_SET_AUTORELOAD(a->htim, (uint32_t)(r->period_us * (SystemCoreClock / 1000000.0f)) - 1u);
    HAL_TIM_OC_Start_IT(a->htim, TIM_CHANNEL_1);

    return 0;
}

/* TIM 更新中断里调：产生一个脉冲、减一步、走完就停。 */
void axis_step_isr(int axis)
{
    axrun_t *r = &RUN[axis];
    if (!r->running) {
        return;
    }
    if (--r->remaining <= 0) {
        r->running = 0;
        HAL_TIM_OC_Stop_IT(AX[axis].htim, TIM_CHANNEL_1);
        return;
    }
    /* TODO: 按剩余步数重算 period_us（加速 -> 匀速 -> 减速三段） */
    __HAL_TIM_SET_AUTORELOAD(AX[axis].htim,
                             (uint32_t)(r->period_us * (SystemCoreClock / 1000000.0f)) - 1u);
}

static int hal_is_moving(int axis)
{
    if (axis < 0 || axis >= AX_COUNT) {
        return 0;
    }
    return RUN[axis].running;
}

static float hal_position_mm(int axis)
{
    if (axis < 0 || axis >= AX_COUNT) {
        return 0.0f;
    }
    /* TODO: g_pos_steps 由 axis_step_isr 在每次真正发出脉冲时累加。
     * 现在还没接上——骨架阶段这个分支返回 0，上了板第一步就要补。 */
    return (float)g_pos_steps[axis] / AX[axis].steps_per_mm;
}

static float hal_max_mm(int axis)
{
    static const float LIM[AX_COUNT] = { 273.0f, 200.0f, 80.0f, 40.0f };
    return (axis >= 0 && axis < AX_COUNT) ? LIM[axis] : 0.0f;
}

static void hal_stop_all(void)
{
    int a;
    for (a = 0; a < AX_COUNT; a++) {
        RUN[a].running = 0;
        HAL_TIM_OC_Stop_IT(AX[a].htim, TIM_CHANNEL_1);
    }
}

/* ================================================================== 回零 */

static volatile int g_homing;
static volatile int g_home_failed;

static int hal_home(void)
{
    /* TODO: 四轴依次回零（不要同时，免得同时撞限位抢电源）。
     * 流程：清 g_home_failed -> 让某轴以低速往负方向走 ->
     *       限位触发 -> 停车 -> 后退 2mm -> 再慢速前进到触发 ->
     *       把该轴位置清零 -> 下一轴。
     * 若走完 max_mm + 余量还没触发，置 g_home_failed = 1。 */
    g_home_failed = 0;
    g_homing = 1;
    return 0;
}

static int hal_home_failed(void)
{
    return g_home_failed;
}

/* ================================================================== 夹爪 */

static float hal_grip_actual_mm(void)
{
    /* 堵转即停，所以这里返回的是**实际停下的位置**，不是指令里的目标。
     * 上位机拿它和 grip_hold_min_mm 比，判断有没有夹到件。 */
    return g_pos_steps[AX_GRIP] / AX[AX_GRIP].steps_per_mm;
}

/* TMC2209 写寄存器（单线 UART，带 CRC）。
 * StallGuard 只需要配这几个：
 *   SGTHRS  堵转阈值，越大越灵敏。先用 60 试，按实测调。
 *   TCOOLTHRS 低速阈值，低于它才启用 StallGuard（回零和夹爪都在低速）
 *   IHOLD_IRUN 保持电流 / 运行电流 —— 夹爪这两个要给得保守，压坏件就麻烦
 * 详见 TMC2209 数据手册 §9。 */
static void tmc_write(uint8_t addr, uint8_t reg, uint32_t val)
{
    uint8_t frame[8];
    frame[0] = 0x05;                 /* 同步 */
    frame[1] = addr;
    frame[2] = (uint8_t)(reg | 0x80);/* 0x80 = 写 */
    frame[3] = (uint8_t)(val >> 24);
    frame[4] = (uint8_t)(val >> 16);
    frame[5] = (uint8_t)(val >> 8);
    frame[6] = (uint8_t)val;
    frame[7] = 0;                    /* TODO: CRC8-ATM */
    /* TODO: HAL_UART_Transmit(&htmc, frame, 8, 10) */
    (void)frame;
}

void tmc_configure(void)
{
    int a;
    for (a = 0; a < AX_COUNT; a++) {
        tmc_write(AX[a].tmc_addr, 0x6A /* SGTHRS */, 60);
        tmc_write(AX[a].tmc_addr, 0x6D /* TCOOLTHRS */, 20000);
        /* 夹爪电流单独给低一点，别把工件压坏 */
        tmc_write(AX[a].tmc_addr, 0x10 /* IHOLD_IRUN */,
                  (a == AX_GRIP) ? 0x00080404u : 0x000C0C0Cu);
    }
}

/* ================================================================== 串口 */

/* UART 用 IDLE 中断 + DMA 收不定长行：一次 DMA 收一批，检测到总线空闲
 * 就说明一行收完了。比逐字节中断省 CPU，也不会被 115200 的中断频率拖住。 */
void HAL_UARTEx_RxEventCallback(UART_HandleTypeDef *huart, uint16_t size)
{
    static char line[PROTO_LINE_MAX];

    if (huart != &huart1) {
        return;
    }
    if (size < sizeof line) {
        memcpy(line, rx_dma_buf, size);
        line[size] = '\0';
        machine_on_line(&g_machine, line, HAL_GetTick());
    } else {
        /* 超长行直接丢，别让一行噪声占满缓冲 */
        HAL_UART_AbortReceive(huart);
    }
    HAL_UARTEx_ReceiveToIdle_DMA(&huart1, rx_dma_buf, sizeof rx_dma_buf);
}

/* ================================================================== 主程序 */

/* hal.h 那张函数表的实现。machine.c 只认这张表，不认外设。 */
static const hal_t HAL_TABLE = {
    hal_reply,
    hal_begin_move,
    hal_is_moving,
    hal_position_mm,
    hal_max_mm,
    hal_home,
    hal_home_failed,
    hal_stop_all,
    hal_grip_actual_mm,
};

int main(void)
{
    static const machine_cfg_t CFG = {
        .steps_per_mm    = { 50.93f, 50.93f, 127.3f, 63.66f },
        .vmax_mm_s       = { 200.0f, 200.0f, 100.0f, 60.0f },
        .accel_mm_s2     = 500.0f,
        .move_timeout_ms = 15000u,
        .grip_timeout_ms = 3000u,
        .home_timeout_ms = 20000u,
    };

    HAL_Init();
    SystemClock_Config();
    MX_GPIO_Init();
    MX_DMA_Init();
    MX_TIM2_Init();
    MX_TIM3_Init();
    MX_TIM4_Init();
    MX_TIM5_Init();
    MX_USART1_UART_Init();
    MX_USART3_UART_Init();

    tmc_configure();
    machine_init(&g_machine, &HAL_TABLE, &CFG, HAL_GetTick());
    HAL_UARTEx_ReceiveToIdle_DMA(&huart1, rx_dma_buf, sizeof rx_dma_buf);

    while (1) {
        machine_tick(&g_machine, HAL_GetTick());
        /* 空闲时喂狗。真卡死了让看门狗复位，而不是留着一个不动的机器。 */
        HAL_IWDG_Refresh(&hiwdg);
    }
}
