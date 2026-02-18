/**
 * D3.js topology tree rendering for Tachyon Management System.
 */

function renderTopologyTree(topology) {
    const container = document.getElementById('treeContainer');
    const tooltip = document.getElementById('tooltip');

    // Clear previous content (except empty state)
    const existingSvg = container.querySelector('svg');
    if (existingSvg) {
        existingSvg.remove();
    }

    if (!topology || !topology.aps || topology.aps.length === 0) {
        return;
    }

    // Convert topology to hierarchical structure for D3
    const treeData = {
        name: 'Network',
        type: 'root',
        children: topology.aps.map(ap => ({
            name: ap.system_name || ap.ip,
            type: 'ap',
            data: ap,
            children: ap.cpes.map(cpe => ({
                name: cpe.system_name || cpe.ip,
                type: 'cpe',
                health: cpe.signal_health,
                data: cpe,
            })),
        })),
    };

    // Set dimensions
    const margin = { top: 40, right: 120, bottom: 40, left: 120 };
    const width = Math.max(800, container.clientWidth - 40);
    const nodeHeight = 60;
    const totalNodes = topology.aps.reduce((sum, ap) => sum + ap.cpes.length + 1, 0) + 1;
    const height = Math.max(500, totalNodes * nodeHeight);

    // Create SVG
    const svg = d3.select(container)
        .append('svg')
        .attr('width', width)
        .attr('height', height)
        .append('g')
        .attr('transform', `translate(${margin.left},${margin.top})`);

    // Create tree layout
    const treeLayout = d3.tree()
        .size([height - margin.top - margin.bottom, width - margin.left - margin.right - 200]);

    // Create hierarchy and compute layout
    const root = d3.hierarchy(treeData);
    treeLayout(root);

    // Create links (lines)
    svg.selectAll('.link')
        .data(root.links())
        .enter()
        .append('path')
        .attr('class', 'link')
        .attr('d', d3.linkHorizontal()
            .x(d => d.y)
            .y(d => d.x));

    // Create nodes
    const nodes = svg.selectAll('.node')
        .data(root.descendants())
        .enter()
        .append('g')
        .attr('class', d => {
            if (d.data.type === 'root') return 'node node--root';
            if (d.data.type === 'ap') return 'node node--ap';
            return `node node--cpe-${d.data.health || 'red'}`;
        })
        .attr('transform', d => `translate(${d.y},${d.x})`);

    // Add circles to nodes
    nodes.append('circle')
        .attr('r', d => {
            if (d.data.type === 'root') return 15;
            if (d.data.type === 'ap') return 12;
            return 10;
        })
        .on('mouseover', (event, d) => showTooltip(event, d, tooltip))
        .on('mouseout', () => hideTooltip(tooltip))
        .on('click', (event, d) => {
            if (d.data.type === 'cpe' && d.data.data) {
                // Could navigate to device detail page
                console.log('Clicked CPE:', d.data.data);
            }
        });

    // Add labels
    nodes.append('text')
        .attr('dy', '0.35em')
        .attr('x', d => {
            if (d.data.type === 'root') return 20;
            return d.children ? -15 : 15;
        })
        .attr('text-anchor', d => {
            if (d.data.type === 'root') return 'start';
            return d.children ? 'end' : 'start';
        })
        .text(d => truncate(d.data.name, 20));

    // Style root node
    svg.selectAll('.node--root circle')
        .style('fill', '#64748b')
        .style('stroke', '#94a3b8');
}

function truncate(text, maxLength) {
    if (!text) return '';
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength - 3) + '...';
}

function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function showTooltip(event, d, tooltip) {
    if (d.data.type === 'root') return;

    let content = '';

    if (d.data.type === 'ap') {
        const ap = d.data.data;
        content = `
            <h4>${escapeHtml(ap.system_name) || 'Access Point'}</h4>
            <div class="tooltip-row">
                <span class="tooltip-label">IP</span>
                <span class="tooltip-value">${escapeHtml(ap.ip)}</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">MAC</span>
                <span class="tooltip-value">${escapeHtml(ap.mac) || 'N/A'}</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">Model</span>
                <span class="tooltip-value">${escapeHtml(ap.model) || 'N/A'}</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">Firmware</span>
                <span class="tooltip-value">${escapeHtml(ap.firmware_version) || 'N/A'}</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">CPEs</span>
                <span class="tooltip-value">${escapeHtml(ap.cpe_count)}</span>
            </div>
            ${ap.error ? `<div class="tooltip-row" style="color: #ef4444;">
                <span class="tooltip-label">Error</span>
                <span class="tooltip-value">${escapeHtml(ap.error)}</span>
            </div>` : ''}
        `;
    } else if (d.data.type === 'cpe') {
        const cpe = d.data.data;
        const healthColor = cpe.signal_health === 'green' ? '#10b981' :
                           cpe.signal_health === 'yellow' ? '#f59e0b' : '#ef4444';
        content = `
            <h4>${escapeHtml(cpe.system_name) || 'CPE'}</h4>
            <div class="tooltip-row">
                <span class="tooltip-label">IP</span>
                <span class="tooltip-value">${escapeHtml(cpe.ip)}</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">MAC</span>
                <span class="tooltip-value">${escapeHtml(cpe.mac) || 'N/A'}</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">Model</span>
                <span class="tooltip-value">${escapeHtml(cpe.model) || 'N/A'}</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">Signal</span>
                <span class="tooltip-value" style="color: ${healthColor}">${cpe.primary_signal?.toFixed(1) || 'N/A'} dBm</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">Distance</span>
                <span class="tooltip-value">${cpe.link_distance?.toFixed(0) || 'N/A'} m</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">TX/RX Rate</span>
                <span class="tooltip-value">${cpe.tx_rate?.toFixed(0) || 'N/A'} / ${cpe.rx_rate?.toFixed(0) || 'N/A'} Mbps</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">MCS</span>
                <span class="tooltip-value">${escapeHtml(cpe.mcs) ?? 'N/A'}</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">Uptime</span>
                <span class="tooltip-value">${formatUptime(cpe.link_uptime)}</span>
            </div>
        `;
    }

    tooltip.innerHTML = content;
    tooltip.classList.add('visible');

    // Position tooltip
    const tooltipRect = tooltip.getBoundingClientRect();
    let left = event.pageX + 15;
    let top = event.pageY - 10;

    // Keep tooltip in viewport
    if (left + tooltipRect.width > window.innerWidth) {
        left = event.pageX - tooltipRect.width - 15;
    }
    if (top + tooltipRect.height > window.innerHeight) {
        top = event.pageY - tooltipRect.height + 10;
    }

    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
}

function hideTooltip(tooltip) {
    tooltip.classList.remove('visible');
}

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
