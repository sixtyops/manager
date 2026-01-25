/**
 * Chart.js signal monitoring for SixtyOps.
 */

// Signal health thresholds (dBm)
const SIGNAL_THRESHOLD_GREEN = -65;
const SIGNAL_THRESHOLD_YELLOW = -75;

/**
 * Get color for signal health.
 * @param {string} health - Signal health: 'green', 'yellow', or 'red'
 * @returns {object} - Object with background and border colors
 */
function getHealthColors(health) {
    const colors = {
        green: { background: '#10b981', border: '#34d399' },
        yellow: { background: '#f59e0b', border: '#fbbf24' },
        red: { background: '#ef4444', border: '#f87171' },
    };
    return colors[health] || colors.red;
}

/**
 * Classify signal strength into health category.
 * @param {number|null} signal - Signal in dBm
 * @returns {string} - 'green', 'yellow', or 'red'
 */
function classifySignal(signal) {
    if (signal === null || signal === undefined) return 'red';
    if (signal > SIGNAL_THRESHOLD_GREEN) return 'green';
    if (signal >= SIGNAL_THRESHOLD_YELLOW) return 'yellow';
    return 'red';
}

/**
 * Format a number for display.
 * @param {number|null} value - The value to format
 * @param {number} decimals - Number of decimal places
 * @param {string} suffix - Suffix to append (e.g., 'dBm', 'm')
 * @returns {string} - Formatted string or 'N/A'
 */
function formatValue(value, decimals = 1, suffix = '') {
    if (value === null || value === undefined) return 'N/A';
    return `${value.toFixed(decimals)}${suffix ? ' ' + suffix : ''}`;
}

/**
 * Format uptime in seconds to human-readable string.
 * @param {number|null} seconds - Uptime in seconds
 * @returns {string} - Formatted string like "2d 5h" or "45m"
 */
function formatUptime(seconds) {
    if (seconds === null || seconds === undefined) return 'N/A';

    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);

    if (days > 0) {
        return `${days}d ${hours}h`;
    }
    if (hours > 0) {
        return `${hours}h ${minutes}m`;
    }
    return `${minutes}m`;
}

/**
 * Create chart configuration for signal vs distance scatter plot.
 * @param {Array} data - Array of CPE data points
 * @returns {object} - Chart.js configuration object
 */
function createSignalChartConfig(data) {
    return {
        type: 'scatter',
        data: {
            datasets: [{
                label: 'CPEs',
                data: data,
                pointBackgroundColor: (context) => {
                    const point = context.raw;
                    if (!point || !point.cpe) return '#64748b';
                    return getHealthColors(point.cpe.signal_health).background;
                },
                pointBorderColor: (context) => {
                    const point = context.raw;
                    if (!point || !point.cpe) return '#94a3b8';
                    return getHealthColors(point.cpe.signal_health).border;
                },
                pointRadius: 8,
                pointHoverRadius: 12,
                pointBorderWidth: 2,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'nearest',
            },
            scales: {
                x: {
                    title: {
                        display: true,
                        text: 'Distance (meters)',
                        color: '#94a3b8',
                        font: { size: 14 },
                    },
                    grid: {
                        color: '#334155',
                    },
                    ticks: {
                        color: '#94a3b8',
                    },
                    beginAtZero: true,
                },
                y: {
                    title: {
                        display: true,
                        text: 'Signal Strength (dBm)',
                        color: '#94a3b8',
                        font: { size: 14 },
                    },
                    grid: {
                        color: '#334155',
                    },
                    ticks: {
                        color: '#94a3b8',
                    },
                    min: -100,
                    max: -30,
                    reverse: false,
                },
            },
            plugins: {
                legend: {
                    display: false,
                },
                tooltip: {
                    enabled: true,
                    backgroundColor: '#1e293b',
                    titleColor: '#f1f5f9',
                    bodyColor: '#e2e8f0',
                    borderColor: '#334155',
                    borderWidth: 1,
                    padding: 12,
                    displayColors: false,
                    callbacks: {
                        title: (tooltipItems) => {
                            const item = tooltipItems[0];
                            if (!item || !item.raw || !item.raw.cpe) return '';
                            const cpe = item.raw.cpe;
                            return cpe.system_name || cpe.ip;
                        },
                        label: (context) => {
                            const cpe = context.raw?.cpe;
                            if (!cpe) return '';
                            return [
                                `IP: ${cpe.ip}`,
                                `Signal: ${formatValue(cpe.primary_signal, 1, 'dBm')}`,
                                `Distance: ${formatValue(cpe.link_distance, 0, 'm')}`,
                                `TX Rate: ${formatValue(cpe.tx_rate, 0, 'Mbps')}`,
                                `RX Rate: ${formatValue(cpe.rx_rate, 0, 'Mbps')}`,
                            ];
                        },
                    },
                },
            },
        },
    };
}

/**
 * Draw threshold lines on the chart.
 * This is a Chart.js plugin.
 */
const thresholdLinesPlugin = {
    id: 'thresholdLines',
    afterDraw: (chart) => {
        const ctx = chart.ctx;
        const yAxis = chart.scales.y;
        const xAxis = chart.scales.x;

        if (!yAxis || !xAxis) return;

        // Draw -65 dBm line (green/yellow threshold)
        const y65 = yAxis.getPixelForValue(SIGNAL_THRESHOLD_GREEN);
        ctx.save();
        ctx.beginPath();
        ctx.setLineDash([6, 4]);
        ctx.strokeStyle = '#10b981';
        ctx.lineWidth = 2;
        ctx.moveTo(xAxis.left, y65);
        ctx.lineTo(xAxis.right, y65);
        ctx.stroke();

        // Add label
        ctx.fillStyle = '#10b981';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'right';
        ctx.fillText('-65 dBm', xAxis.right - 5, y65 - 5);
        ctx.restore();

        // Draw -75 dBm line (yellow/red threshold)
        const y75 = yAxis.getPixelForValue(SIGNAL_THRESHOLD_YELLOW);
        ctx.save();
        ctx.beginPath();
        ctx.setLineDash([6, 4]);
        ctx.strokeStyle = '#ef4444';
        ctx.lineWidth = 2;
        ctx.moveTo(xAxis.left, y75);
        ctx.lineTo(xAxis.right, y75);
        ctx.stroke();

        // Add label
        ctx.fillStyle = '#ef4444';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'right';
        ctx.fillText('-75 dBm', xAxis.right - 5, y75 - 5);
        ctx.restore();
    },
};

// Register the plugin globally
if (typeof Chart !== 'undefined') {
    Chart.register(thresholdLinesPlugin);
}

/**
 * Prepare CPE data for the scatter chart.
 * @param {Array} cpes - Array of CPE objects
 * @returns {Array} - Array of chart data points
 */
function prepareChartData(cpes) {
    return cpes.map(cpe => ({
        x: cpe.link_distance || 0,
        y: cpe.primary_signal || -100,
        cpe: cpe,
    }));
}

/**
 * Sort CPEs by signal strength (weakest first).
 * @param {Array} cpes - Array of CPE objects
 * @returns {Array} - Sorted array
 */
function sortCPEsBySignal(cpes) {
    return [...cpes].sort((a, b) => {
        const aSignal = a.primary_signal ?? -100;
        const bSignal = b.primary_signal ?? -100;
        return aSignal - bSignal;
    });
}

/**
 * Count CPEs by signal health.
 * @param {Array} cpes - Array of CPE objects
 * @returns {object} - Object with green, yellow, red counts
 */
function countByHealth(cpes) {
    const counts = { green: 0, yellow: 0, red: 0 };
    cpes.forEach(cpe => {
        const health = cpe.signal_health || classifySignal(cpe.primary_signal);
        counts[health] = (counts[health] || 0) + 1;
    });
    return counts;
}
