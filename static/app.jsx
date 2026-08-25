const { useEffect, useMemo, useRef, useState } = React;

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Request failed for ${url}`);
  }
  return response.json();
}

function fmt(value, fallback = "-") {
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  return String(value);
}

function fmtNumber(value, digits = 2, fallback = "-") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return fallback;
  }
  return Number(value).toFixed(digits);
}

function fmtTime(value) {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

function Pill({ status }) {
  const value = (status || "unknown").toLowerCase();
  return <span className={`pill ${value}`}>{value}</span>;
}

function DataTable({ headers, rows }) {
  return (
    <table>
      <thead>
        <tr>
          {headers.map((header) => (
            <th key={header}>{header}</th>
          ))}
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  );
}

function projectPoint(point, rotation, projection) {
  const { yaw, pitch } = rotation;
  const cosY = Math.cos(yaw);
  const sinY = Math.sin(yaw);
  const cosX = Math.cos(pitch);
  const sinX = Math.sin(pitch);

  const x1 = point.x * cosY + point.z * sinY;
  const z1 = -point.x * sinY + point.z * cosY;
  const y1 = point.y * cosX - z1 * sinX;
  const z2 = point.y * sinX + z1 * cosX;

  const perspective = projection.focalLength / (projection.focalLength + z2 * projection.depth);
  return {
    sx: projection.cx + x1 * projection.scale * perspective,
    sy: projection.cy - y1 * projection.scale * perspective,
    depth: z2,
    perspective,
  };
}

function D3CallSpaceGraph({ points }) {
  const svgRef = useRef(null);
  const wrapperRef = useRef(null);
  const [rotation, setRotation] = useState({ yaw: 0.68, pitch: 0.42 });
  const [zoom, setZoom] = useState(1);
  const [tooltip, setTooltip] = useState(null);

  const normalized = useMemo(() => {
    const xExtent = d3.extent(points, (p) => Number(p.x_pitch_hz || 0));
    const yExtent = d3.extent(points, (p) => Number(p.y_timbre_centroid_hz || 0));
    const zExtent = d3.extent(points, (p) => Number(p.z_tonal_spread_hz || 0));

    const xScale = d3.scaleLinear().domain(xExtent[0] === xExtent[1] ? [0, xExtent[1] || 1] : xExtent).range([-1, 1]);
    const yScale = d3.scaleLinear().domain(yExtent[0] === yExtent[1] ? [0, yExtent[1] || 1] : yExtent).range([-1, 1]);
    const zScale = d3.scaleLinear().domain(zExtent[0] === zExtent[1] ? [0, zExtent[1] || 1] : zExtent).range([-1, 1]);

    return points.map((p) => ({
      raw: p,
      x: xScale(Number(p.x_pitch_hz || 0)),
      y: yScale(Number(p.y_timbre_centroid_hz || 0)),
      z: zScale(Number(p.z_tonal_spread_hz || 0)),
      confidence: Number(p.confidence || 0),
      energy: Number(p.energy_rms || 0),
    }));
  }, [points]);

  useEffect(() => {
    const svg = d3.select(svgRef.current);
    const wrapper = wrapperRef.current;
    if (!wrapper) {
      return;
    }

    const width = wrapper.clientWidth;
    const height = Math.min(Math.max(wrapper.clientHeight, 360), 600);
    svg.attr("viewBox", `0 0 ${width} ${height}`);

    const projection = {
      cx: width / 2,
      cy: height / 2,
      scale: Math.min(width, height) * 0.28 * zoom,
      focalLength: 3,
      depth: 0.9,
    };

    svg.selectAll("*").remove();

    const graphLayer = svg.append("g");

    const axisVectors = [
      { key: "x", label: "Pitch", point: { x: 1.2, y: 0, z: 0 }, color: "#d8572a" },
      { key: "y", label: "Timbre", point: { x: 0, y: 1.2, z: 0 }, color: "#2f7f79" },
      { key: "z", label: "Spread", point: { x: 0, y: 0, z: 1.2 }, color: "#3a5db5" },
    ];

    const originProjected = projectPoint({ x: 0, y: 0, z: 0 }, rotation, projection);

    axisVectors.forEach((axis) => {
      const p = projectPoint(axis.point, rotation, projection);
      graphLayer
        .append("line")
        .attr("x1", originProjected.sx)
        .attr("y1", originProjected.sy)
        .attr("x2", p.sx)
        .attr("y2", p.sy)
        .attr("stroke", axis.color)
        .attr("stroke-width", 2.2)
        .attr("opacity", 0.9);

      graphLayer
        .append("text")
        .attr("x", p.sx + 8)
        .attr("y", p.sy - 6)
        .attr("class", "axis-label")
        .style("fill", axis.color)
        .text(axis.label);
    });

    const bounds = [-1, 1];
    const cubeLines = [];
    bounds.forEach((x) => {
      bounds.forEach((y) => {
        cubeLines.push([{ x, y, z: -1 }, { x, y, z: 1 }]);
      });
      bounds.forEach((z) => {
        cubeLines.push([{ x, y: -1, z }, { x, y: 1, z }]);
      });
    });
    bounds.forEach((y) => {
      bounds.forEach((z) => {
        cubeLines.push([{ x: -1, y, z }, { x: 1, y, z }]);
      });
    });

    cubeLines.forEach(([a, b]) => {
      const pa = projectPoint(a, rotation, projection);
      const pb = projectPoint(b, rotation, projection);
      graphLayer
        .append("line")
        .attr("x1", pa.sx)
        .attr("y1", pa.sy)
        .attr("x2", pb.sx)
        .attr("y2", pb.sy)
        .attr("stroke", "rgba(23, 32, 38, 0.18)")
        .attr("stroke-width", 1);
    });

    const colorScale = d3.scaleSequential(d3.interpolateTurbo).domain([0, 1]);

    const projectedPoints = normalized
      .map((item) => {
        const projected = projectPoint(item, rotation, projection);
        return {
          ...item,
          ...projected,
        };
      })
      .sort((a, b) => a.depth - b.depth);

    graphLayer
      .selectAll("circle.point")
      .data(projectedPoints)
      .join("circle")
      .attr("class", "point")
      .attr("cx", (d) => d.sx)
      .attr("cy", (d) => d.sy)
      .attr("r", (d) => Math.max(2.4, 2.4 + d.energy * 120 * d.perspective))
      .attr("fill", (d) => colorScale(d.confidence))
      .attr("fill-opacity", (d) => Math.max(0.3, 0.45 + d.perspective * 0.45))
      .attr("stroke", "rgba(255,255,255,0.7)")
      .attr("stroke-width", 0.5)
      .on("mousemove", (event, d) => {
        const [mx, my] = d3.pointer(event, wrapper);
        setTooltip({
          x: mx + 16,
          y: my + 16,
          species: d.raw.species || "Unknown",
          pitch: d.raw.x_pitch_hz || 0,
          centroid: d.raw.y_timbre_centroid_hz || 0,
          spread: d.raw.z_tonal_spread_hz || 0,
          energy: d.raw.energy_rms || 0,
          confidence: d.raw.confidence || 0,
        });
      })
      .on("mouseleave", () => {
        setTooltip(null);
      });
  }, [normalized, rotation, zoom]);

  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) {
      return;
    }

    let dragging = false;
    let lastX = 0;
    let lastY = 0;

    const onDown = (event) => {
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
    };

    const onMove = (event) => {
      if (!dragging) {
        return;
      }
      const dx = event.clientX - lastX;
      const dy = event.clientY - lastY;
      lastX = event.clientX;
      lastY = event.clientY;
      setRotation((prev) => ({
        yaw: prev.yaw + dx * 0.008,
        pitch: Math.max(-1.2, Math.min(1.2, prev.pitch + dy * 0.008)),
      }));
    };

    const onUp = () => {
      dragging = false;
    };

    const onWheel = (event) => {
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.08 : 0.92;
      setZoom((prev) => Math.max(0.6, Math.min(2.4, prev * factor)));
    };

    wrapper.addEventListener("pointerdown", onDown);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    wrapper.addEventListener("wheel", onWheel, { passive: false });

    return () => {
      wrapper.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      wrapper.removeEventListener("wheel", onWheel);
    };
  }, []);

  return (
    <div className="plot d3-plot" ref={wrapperRef}>
      <svg ref={svgRef} className="plot-svg" />
      <div className="graph-hint">Drag to rotate, wheel to zoom</div>
      {tooltip && (
        <div className="tooltip" style={{ left: tooltip.x, top: tooltip.y }}>
          <strong>{tooltip.species}</strong>
          <div>Pitch: {fmtNumber(tooltip.pitch)} Hz</div>
          <div>Centroid: {fmtNumber(tooltip.centroid)} Hz</div>
          <div>Spread: {fmtNumber(tooltip.spread)} Hz</div>
          <div>Energy: {fmtNumber(tooltip.energy, 5)}</div>
          <div>Confidence: {fmtNumber(tooltip.confidence, 3)}</div>
        </div>
      )}
    </div>
  );
}

function App() {
  const [status, setStatus] = useState({});
  const [audio, setAudio] = useState({ count: 0, items: [] });
  const [processed, setProcessed] = useState({ count: 0, items: [] });
  const [space, setSpace] = useState({ count: 0, items: [] });
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function refresh() {
      try {
        const [statusJson, audioJson, processedJson, spaceJson] = await Promise.all([
          getJson("/api/status"),
          getJson("/api/audio"),
          getJson("/api/processed"),
          getJson("/api/space"),
        ]);

        if (cancelled) {
          return;
        }

        setStatus(statusJson || {});
        setAudio(audioJson || { count: 0, items: [] });
        setProcessed(processedJson || { count: 0, items: [] });
        setSpace(spaceJson || { count: 0, items: [] });
        setError("");
      } catch (err) {
        if (cancelled) {
          return;
        }
        setError(err.message || "Unknown dashboard error");
      }
    }

    refresh();
    const timer = setInterval(refresh, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const state = status.state || "unknown";
  const running = Boolean(status.analyzerRunning);
  const statusLabel = running ? "Analyzer running" : "Analyzer not active";

  return (
    <>
      <div className="bg-glow bg-one"></div>
      <div className="bg-glow bg-two"></div>

      <header className="hero">
        <div>
          <h1>Bird Analyzer Flight Deck</h1>
          <p>Monitor recorded audio, processed detections, and acoustic behavior in a D3-driven 3D call-space graph.</p>
        </div>
        <div className="status-badge">
          {error ? (
            <span>Dashboard error: {error}</span>
          ) : (
            <>
              {statusLabel} <Pill status={state} /> | Queue: {fmt(status.queue_size, "0")} | Last heartbeat: {fmtTime(status.heartbeat)}
            </>
          )}
        </div>
      </header>

      <main className="grid">
        <section className="panel stats-panel">
          <h2>Pipeline Summary</h2>
          <div className="stat-cards">
            <div className="card">
              <span>Recorded Sounds</span>
              <strong>{fmt(audio.count, "0")}</strong>
            </div>
            <div className="card">
              <span>Processed Sounds</span>
              <strong>{fmt(processed.count, "0")}</strong>
            </div>
            <div className="card">
              <span>3D Points</span>
              <strong>{fmt(space.count, "0")}</strong>
            </div>
          </div>
        </section>

        <section className="panel graph-panel">
          <h2>3D Bird Call Space</h2>
          <p className="sub">X: Dominant pitch, Y: Timbre centroid, Z: Tonal spread, Color: confidence, Radius: energy</p>
          <D3CallSpaceGraph points={space.items || []} />
        </section>

        <section className="panel table-panel">
          <h2>Recorded Audio Index</h2>
          <DataTable
            headers={["Time", "Status", "Top Species", "Confidence", "File"]}
            rows={(audio.items || []).slice(0, 200).map((item, idx) => (
              <tr key={`${item.recording_id || item._id || "audio"}-${idx}`}>
                <td>{fmtTime(item.timestamp)}</td>
                <td>
                  <Pill status={item.status || "recorded"} />
                </td>
                <td>{fmt(item.top_prediction)}</td>
                <td>{fmtNumber(item.top_confidence, 3)}</td>
                <td title={fmt(item.file_path)}>{fmt(item.file_path)}</td>
              </tr>
            ))}
          />
        </section>

        <section className="panel table-panel">
          <h2>Processed Metadata Index</h2>
          <DataTable
            headers={["Time", "Name", "Scientific", "Confidence", "Family"]}
            rows={(processed.items || []).slice(0, 200).map((item, idx) => (
              <tr key={`${item._id || "processed"}-${idx}`}>
                <td>{fmtTime(item.timestamp)}</td>
                <td>{fmt(item.name)}</td>
                <td>{fmt(item.sciName)}</td>
                <td>{fmtNumber(item.confidence, 3)}</td>
                <td>{fmt(item.family)}</td>
              </tr>
            ))}
          />
        </section>
      </main>
    </>
  );
}

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(<App />);
