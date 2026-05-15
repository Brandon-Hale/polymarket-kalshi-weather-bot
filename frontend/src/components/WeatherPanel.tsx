import type { WeatherForecast, WeatherSignal } from '../types'
import { platformStyles } from '../utils'

interface Props {
  forecasts: WeatherForecast[]
  signals: WeatherSignal[]
}

export function WeatherPanel({ forecasts, signals }: Props) {
  if (forecasts.length === 0 && signals.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-neutral-600 text-[10px]">
        No weather data
      </div>
    )
  }

  const signalsByCity = new Map<string, WeatherSignal[]>()
  signals.forEach(s => {
    const existing = signalsByCity.get(s.city_key) || []
    existing.push(s)
    signalsByCity.set(s.city_key, existing)
  })

  // Sort cities: actionable signals first, then by absolute edge magnitude.
  // Cities without signals fall to the bottom, ordered alphabetically.
  const sortedForecasts = [...forecasts].sort((a, b) => {
    const aSigs = signalsByCity.get(a.city_key) || []
    const bSigs = signalsByCity.get(b.city_key) || []
    const aActionable = aSigs.some(s => s.actionable) ? 1 : 0
    const bActionable = bSigs.some(s => s.actionable) ? 1 : 0
    if (aActionable !== bActionable) return bActionable - aActionable
    const aEdge = aSigs.length > 0 ? Math.max(...aSigs.map(s => Math.abs(s.edge))) : 0
    const bEdge = bSigs.length > 0 ? Math.max(...bSigs.map(s => Math.abs(s.edge))) : 0
    if (aEdge !== bEdge) return bEdge - aEdge
    return a.city_name.localeCompare(b.city_name)
  })

  return (
    <div className="grid grid-cols-2 gap-x-3 gap-y-1 overflow-y-auto max-h-full px-1 py-1">
      {sortedForecasts.map(f => {
        const citySignals = signalsByCity.get(f.city_key) || []
        const actionable = citySignals.filter(s => s.actionable)
        const bestEdge = citySignals.length > 0
          ? citySignals.reduce((a, b) => Math.abs(a.edge) > Math.abs(b.edge) ? a : b)
          : null

        return (
          <div
            key={f.city_key}
            className={`flex items-center gap-2 px-2 py-1.5 min-w-0 ${
              actionable.length > 0 ? 'border-l-2 border-l-green-500 bg-green-500/5' : 'border-l-2 border-l-transparent'
            }`}
          >
            <div className="w-14 shrink-0">
              <div className="text-[10px] font-medium text-neutral-300 truncate" title={f.city_name}>{f.city_name}</div>
            </div>
            <div className="flex-1 min-w-0 flex items-center gap-2 text-[10px] tabular-nums">
              <span className="text-neutral-300 shrink-0">
                {f.mean_high.toFixed(0)}F
                <span className="text-neutral-600 ml-0.5">+/-{f.std_high.toFixed(0)}</span>
              </span>
            </div>
            <span className={`w-9 text-right shrink-0 text-[10px] tabular-nums ${f.ensemble_agreement > 0.7 ? 'text-green-500' : 'text-amber-500'}`}>
              {(f.ensemble_agreement * 100).toFixed(0)}%
            </span>
            <div className="w-20 flex items-center justify-end gap-1 shrink-0 text-[10px] tabular-nums">
              {bestEdge && (
                <span className={`tabular-nums ${bestEdge.edge > 0 ? 'text-green-500' : 'text-red-500'}`}>
                  {bestEdge.edge > 0 ? '+' : ''}{(bestEdge.edge * 100).toFixed(1)}%
                </span>
              )}
              {citySignals.length > 0 && citySignals[0].platform && (
                <span className={`platform-badge ${
                  platformStyles[citySignals[0].platform.toLowerCase()]?.badge || 'bg-neutral-800 text-neutral-400 border-neutral-700'
                }`}>
                  {platformStyles[citySignals[0].platform.toLowerCase()]?.icon || '?'}
                </span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
