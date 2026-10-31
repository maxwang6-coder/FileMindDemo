import { createApp } from 'vue'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/card/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/tag/style/css'
import App from './App.vue'
import './style.css'

const root = document.querySelector('#app')
createApp(App, {
  writeMode: root.dataset.writeMode,
  modelMode: root.dataset.modelMode,
}).mount(root)
