import { useEffect, useRef, useMemo, useCallback } from 'react'
import Globe from 'react-globe.gl'
import type { WeatherForecast, WeatherSignal } from '../types'

interface Props {
  forecasts: WeatherForecast[]
  signals: WeatherSignal[]
}

interface CityMarker {
  lat: number
  lng: number
  name: string
  key: string
  forecast: WeatherForecast | null
  bestSignal: WeatherSignal | null
  hasActionable: boolean
}

const CITIES: Record<string, { lat: number; lng: number; name: string }> = {
  // US
  nyc: { lat: 40.7128, lng: -74.006, name: 'NYC' },
  chicago: { lat: 41.8781, lng: -87.6298, name: 'CHI' },
  miami: { lat: 25.7617, lng: -80.1918, name: 'MIA' },
  los_angeles: { lat: 34.0522, lng: -118.2437, name: 'LA' },
  austin: { lat: 30.2672, lng: -97.7431, name: 'AUS' },
  atlanta: { lat: 33.749, lng: -84.388, name: 'ATL' },
  seattle: { lat: 47.6062, lng: -122.3321, name: 'SEA' },
  // China + HK
  beijing: { lat: 39.9042, lng: 116.4074, name: 'BEJ' },
  shanghai: { lat: 31.2304, lng: 121.4737, name: 'SHA' },
  chongqing: { lat: 29.563, lng: 106.5516, name: 'CQG' },
  guangzhou: { lat: 23.1291, lng: 113.2644, name: 'CAN' },
  chengdu: { lat: 30.5728, lng: 104.0668, name: 'CTU' },
  wuhan: { lat: 30.5928, lng: 114.3055, name: 'WUH' },
  hong_kong: { lat: 22.3193, lng: 114.1694, name: 'HKG' },
  shenzhen: { lat: 22.5431, lng: 114.0579, name: 'SZX' },
  // Europe
  london: { lat: 51.5074, lng: -0.1278, name: 'LON' },
  paris: { lat: 48.8566, lng: 2.3522, name: 'PAR' },
  madrid: { lat: 40.4168, lng: -3.7038, name: 'MAD' },
  milan: { lat: 45.4642, lng: 9.19, name: 'MIL' },
  munich: { lat: 48.1351, lng: 11.582, name: 'MUC' },
  amsterdam: { lat: 52.3676, lng: 4.9041, name: 'AMS' },
  warsaw: { lat: 52.2297, lng: 21.0122, name: 'WAW' },
  helsinki: { lat: 60.1699, lng: 24.9384, name: 'HEL' },
  moscow: { lat: 55.7558, lng: 37.6173, name: 'MOW' },
  istanbul: { lat: 41.0082, lng: 28.9784, name: 'IST' },
  ankara: { lat: 39.9334, lng: 32.8597, name: 'ANK' },
}

export function GlobeView({ forecasts, signals }: Props) {
  const globeRef = useRef<any>(null)

  const markers: CityMarker[] = useMemo(() => {
    return Object.entries(CITIES).map(([key, city]) => {
      const forecast = forecasts.find(f => f.city_key === key) || null
      const citySignals = signals.filter(s => s.city_key === key)
      const actionableSignals = citySignals.filter(s => s.actionable)
      const bestSignal = actionableSignals.length > 0
        ? actionableSignals.reduce((a, b) => Math.abs(a.edge) > Math.abs(b.edge) ? a : b)
        : citySignals.length > 0
          ? citySignals.reduce((a, b) => Math.abs(a.edge) > Math.abs(b.edge) ? a : b)
          : null

      return {
        lat: city.lat,
        lng: city.lng,
        name: city.name,
        key,
        forecast,
        bestSignal,
        hasActionable: actionableSignals.length > 0,
      }
    })
  }, [forecasts, signals])

  useEffect(() => {
    if (globeRef.current) {
      globeRef.current.pointOfView({ lat: 39.5, lng: -98.35, altitude: 2.2 }, 1000)
      globeRef.current.controls().autoRotate = true
      globeRef.current.controls().autoRotateSpeed = 0.3
      globeRef.current.controls().enableZoom = false
    }
  }, [])

  const handleInteraction = useCallback(() => {
    if (globeRef.current) {
      globeRef.current.controls().autoRotate = false
      setTimeout(() => {
        if (globeRef.current) {
          globeRef.current.controls().autoRotate = true
        }
      }, 5000)
    }
  }, [])

  const markerElement = useCallback((d: object) => {
    const marker = d as CityMarker
    const el = document.createElement('div')
    el.className = 'city-marker'

    const dotColor = marker.hasActionable ? '#22c55e' : marker.bestSignal ? '#d97706' : '#525252'

    const dot = document.createElement('div')
    dot.className = 'marker-dot'
    dot.style.backgroundColor = dotColor
    dot.style.color = dotColor
    el.appendChild(dot)

    const label = document.createElement('div')
    label.className = 'marker-label'

    const nameSpan = document.createElement('div')
    nameSpan.className = 'marker-name'
    nameSpan.textContent = marker.name
    label.appendChild(nameSpan)

    if (marker.forecast) {
      const tempSpan = document.createElement('div')
      tempSpan.className = 'marker-temp'
      tempSpan.style.color = '#e5e5e5'
      tempSpan.textContent = `${marker.forecast.mean_high.toFixed(0)}F`
      label.appendChild(tempSpan)
    }

    if (marker.bestSignal) {
      const edgeSpan = document.createElement('div')
      edgeSpan.className = 'marker-edge'
      const edgeVal = (marker.bestSignal.edge * 100).toFixed(1)
      edgeSpan.style.color = marker.bestSignal.edge > 0 ? '#22c55e' : '#dc2626'
      edgeSpan.textContent = `${marker.bestSignal.edge > 0 ? '+' : ''}${edgeVal}%`
      label.appendChild(edgeSpan)
    }

    el.appendChild(label)
    return el
  }, [])

  return (
    <div className="globe-container w-full h-full flex items-center justify-center overflow-hidden pt-32">
      <Globe
        ref={globeRef}
        globeImageUrl="//unpkg.com/three-globe/example/img/earth-night.jpg"
        backgroundColor="rgba(0,0,0,0)"
        atmosphereColor="#1a1a2e"
        atmosphereAltitude={0.15}
        htmlElementsData={markers}
        htmlElement={markerElement}
        htmlAltitude={0.01}
        onGlobeClick={handleInteraction}
        width={undefined}
        height={undefined}
      />
    </div>
  )
}
