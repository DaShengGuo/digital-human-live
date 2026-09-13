<script setup>
// 自研虚拟滚动: 只渲染可视窗口内的行(上下各留 overscan 缓冲),
// 支撑长时直播的弹幕/事件流不卡顿。行高固定, 二分可省略, 直接除法定位。
import { computed, ref } from 'vue'

const props = defineProps({
  items: { type: Array, default: () => [] },
  rowHeight: { type: Number, default: 36 },
  height: { type: Number, default: 420 }
})
const scrollTop = ref(0)

const total = computed(() => props.items.length)
const visibleCount = computed(() => Math.ceil(props.height / props.rowHeight) + 8)
const startIdx = computed(() =>
  Math.max(0, Math.floor(scrollTop.value / props.rowHeight) - 4))
const visible = computed(() =>
  props.items.slice(startIdx.value, startIdx.value + visibleCount.value))
const offsetY = computed(() => startIdx.value * props.rowHeight)
const fillerHeight = computed(() => Math.max(0, total.value * props.rowHeight - offsetY.value
  - visible.value.length * props.rowHeight))

function onScroll(e) { scrollTop.value = e.target.scrollTop }
</script>

<template>
  <div class="vlist" :style="{ height: height + 'px' }" @scroll="onScroll">
    <div :style="{ height: offsetY + 'px' }"></div>
    <div v-for="(it, i) in visible" :key="startIdx + i" :style="{ height: rowHeight + 'px' }">
      <slot :item="it" :index="startIdx + i" />
    </div>
    <div :style="{ height: fillerHeight + 'px' }"></div>
  </div>
</template>

<style scoped>
.vlist { overflow-y: auto; border: 1px solid #ebeef5; border-radius: 4px; }
</style>
