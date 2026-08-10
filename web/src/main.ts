import { mount } from 'svelte'
import App from './App.svelte'
import './styles.css'

const target = document.getElementById('app')
if (!target) throw new Error('#app が無い(index.html が壊れている)')

export default mount(App, { target })
