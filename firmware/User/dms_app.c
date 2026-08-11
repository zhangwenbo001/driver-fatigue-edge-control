#include "dms_app.h"
#include "dms_protocol.h"
#include "./BSP/LED/led.h"
#include "./BSP/BEEP/beep.h"

#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"
#include "stream_buffer.h"

#define DMS_RX_BUF_SIZE         64
#define DMS_RX_TASK_PRIO        3
#define DMS_OUTPUT_TASK_PRIO    2
#define DMS_RX_STACK_SIZE       256

#if defined(DMS_H08_TEST)
/* H-08 隔离测试构建：仅当 Keil 工程 H08_TEST target 定义 DMS_H08_TEST 时生效。
   dms_out 任务栈人为缩小为 32 词，确定性填栈（dms_h08_overflow_bomb）真实写穿
   栈底 0xa5 标记，触发 FreeRTOS Method-2 检测。发布构建不定义该宏，行为不变。 */
#define DMS_OUTPUT_STACK_SIZE   32
#define DMS_H08_SINK_BYTES      256
#define DMS_H08_GUARD_BYTES     256
#else
#define DMS_OUTPUT_STACK_SIZE   256
#endif

#define DMS_LINK_TIMEOUT_MS     2000U
#define DMS_OUTPUT_PERIOD_MS    50U

static uint8_t                s_stream_buffer_storage[DMS_RX_BUF_SIZE + 1];
static StaticStreamBuffer_t   s_stream_buffer_struct;
static StreamBufferHandle_t   s_stream_buffer;

static uint8_t                s_queue_storage[sizeof(dms_parsed_frame_t)];
static StaticQueue_t          s_queue_struct;
static QueueHandle_t          s_queue;

static StaticTask_t s_rx_task_tcb;
static StackType_t  s_rx_task_stack[DMS_RX_STACK_SIZE];

#if defined(DMS_H08_TEST)
/* H-08：守卫区置于任务栈更低地址侧。真实栈溢出向下写穿栈数组后先进入守卫区，
   Method-2 在切换点读取 pxStack[0..3] 标记时 TCB 仍完整可读，钩子确定可达。 */
static struct {
    StaticTask_t  tcb;
    uint8_t       guard[DMS_H08_GUARD_BYTES];
    StackType_t   stack[DMS_OUTPUT_STACK_SIZE];
} s_h08_output;
#else
static StaticTask_t s_output_task_tcb;
static StackType_t  s_output_task_stack[DMS_OUTPUT_STACK_SIZE];
#endif

static volatile uint8_t  s_dms_ready;
static volatile uint32_t s_overflow_count;

static void dms_rx_task(void *pvParameters);
static void dms_output_task(void *pvParameters);

#if defined(DMS_H08_TEST)
/* H-08 确定性栈溢出：局部数组真实压栈并写满，穿破任务栈底 0xa5 填充标记；
   随后保持深栈让出，调度器在切换点经 Method-2 调用既有 vApplicationStackOverflowHook。
   不依赖串口负载，发布构建不编译。 */
static void dms_h08_overflow_bomb(void)
{
    volatile uint8_t sink[DMS_H08_SINK_BYTES];
    uint16_t i;
    for (i = 0; i < DMS_H08_SINK_BYTES; i++) {
        sink[i] = (uint8_t)(0x5A ^ (uint8_t)(i & 0x0F));
    }
    vTaskDelay(pdMS_TO_TICKS(50));
    for (;;) { }
}
#endif

void dms_rx_byte_isr(uint8_t byte)
{
    BaseType_t xHigherPriorityTaskWoken = pdFALSE;
    if (!s_dms_ready) return;
    if (s_stream_buffer != NULL) {
        if (xStreamBufferSendFromISR(s_stream_buffer, &byte, 1, &xHigherPriorityTaskWoken)
                != 1) {
            s_overflow_count++;
        }
    }
    portYIELD_FROM_ISR(xHigherPriorityTaskWoken);
}

int dms_app_init(void)
{
    TaskHandle_t rx_handle, out_handle;

    s_overflow_count = 0;
    s_dms_ready = 0;

    s_stream_buffer = xStreamBufferCreateStatic(
        DMS_RX_BUF_SIZE, 1,
        s_stream_buffer_storage,
        &s_stream_buffer_struct);

    s_queue = xQueueCreateStatic(
        1, sizeof(dms_parsed_frame_t),
        s_queue_storage,
        &s_queue_struct);

    rx_handle = xTaskCreateStatic(dms_rx_task, "dms_rx",
        DMS_RX_STACK_SIZE, NULL,
        DMS_RX_TASK_PRIO,
        s_rx_task_stack, &s_rx_task_tcb);

#if defined(DMS_H08_TEST)
    out_handle = xTaskCreateStatic(dms_output_task, "dms_out",
        DMS_OUTPUT_STACK_SIZE, NULL,
        DMS_OUTPUT_TASK_PRIO,
        s_h08_output.stack, &s_h08_output.tcb);
#else
    out_handle = xTaskCreateStatic(dms_output_task, "dms_out",
        DMS_OUTPUT_STACK_SIZE, NULL,
        DMS_OUTPUT_TASK_PRIO,
        s_output_task_stack, &s_output_task_tcb);
#endif

    if (rx_handle == NULL || out_handle == NULL) {
        return -1;
    }

    s_dms_ready = 1;
    return 0;
}

static void dms_rx_task(void *pvParameters)
{
    (void)pvParameters;
    uint8_t buf[DMS_RX_BUF_SIZE];
    uint16_t idx = 0;
    uint8_t discarding = 0;

    for (;;) {
        uint8_t byte;
        if (xStreamBufferReceive(s_stream_buffer, &byte, 1, portMAX_DELAY) != 1)
            continue;

        if (discarding) {
            if (byte == '\n') {
                discarding = 0;
                idx = 0;
            }
            continue;
        }

        if (idx >= DMS1_FRAME_MAX_BYTES) {
            discarding = 1;
            idx = 0;
            if (byte == '\n') {
                discarding = 0;
            }
            continue;
        }

        buf[idx++] = byte;

        if (idx >= 2 && buf[idx-2] == '\r' && buf[idx-1] == '\n') {
            dms_parsed_frame_t frame = dms_parse(buf, idx);
            if (frame.state != DMS_STATE_INVALID) {
                xQueueOverwrite(s_queue, &frame);
            }
            idx = 0;
        }
    }
}

static void dms_output_task(void *pvParameters)
{
    (void)pvParameters;
#if defined(DMS_H08_TEST)
    dms_h08_overflow_bomb();
#endif
    TickType_t xLastWakeTime = xTaskGetTickCount();
    const TickType_t xPeriod = pdMS_TO_TICKS(DMS_OUTPUT_PERIOD_MS);

    dms_state_t current_display_state = DMS_STATE_BOOT;
    TickType_t last_valid_tick = 0;
    uint8_t has_valid_frame = 0;

    for (;;) {
        vTaskDelayUntil(&xLastWakeTime, xPeriod);

        dms_parsed_frame_t frame;
        if (xQueueReceive(s_queue, &frame, 0) == pdTRUE) {
            current_display_state = frame.state;
            last_valid_tick = xTaskGetTickCount();
            has_valid_frame = 1;
        }

        TickType_t now = xTaskGetTickCount();
        dms_state_t effective;

        if (!has_valid_frame) {
            effective = DMS_STATE_BOOT;
        } else if ((now - last_valid_tick) >= pdMS_TO_TICKS(DMS_LINK_TIMEOUT_MS)) {
            effective = DMS_STATE_LINK_LOST;
        } else {
            effective = current_display_state;
        }

        TickType_t phase_1s = now % pdMS_TO_TICKS(1000);
        TickType_t phase_2s = now % pdMS_TO_TICKS(2000);

        switch (effective) {
        case DMS_STATE_BOOT:
            LED0(1); LED1(1); BEEP_OFF();
            break;
        case DMS_STATE_NORMAL:
            LED0(1); LED1(0); BEEP_OFF();
            break;
        case DMS_STATE_YAWN:
            LED1(1);
            LED0(((phase_1s < pdMS_TO_TICKS(500)) ? 0 : 1));
            if (phase_1s < pdMS_TO_TICKS(100)) { BEEP_ON(); }
            else                               { BEEP_OFF(); }
            break;
        case DMS_STATE_FATIGUE:
            LED0(0); LED1(1);
            if ((phase_1s % pdMS_TO_TICKS(500)) < pdMS_TO_TICKS(250)) { BEEP_ON(); }
            else                                                       { BEEP_OFF(); }
            break;
        case DMS_STATE_UNKNOWN:
            BEEP_OFF();
            if (phase_1s < pdMS_TO_TICKS(500)) { LED0(0); LED1(1); }
            else                               { LED0(1); LED1(0); }
            break;
        case DMS_STATE_LINK_LOST:
            if ((phase_1s % pdMS_TO_TICKS(500)) < pdMS_TO_TICKS(250)) { LED0(0); LED1(1); }
            else                                                       { LED0(1); LED1(0); }
            if (phase_2s < pdMS_TO_TICKS(100)) { BEEP_ON(); }
            else                               { BEEP_OFF(); }
            break;
        default:
            break;
        }
    }
}
