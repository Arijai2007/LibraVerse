(() => {
  const source = document.getElementById('circulation-chart-data');
  if (!source || !window.Chart) return;

  const {labels, series, stock} = JSON.parse(source.textContent);
  const palette = ['#143d34', '#b08a43', '#974c38', '#5a776a'];
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const options = {
    responsive: true,
    maintainAspectRatio: false,
    animation: reduceMotion ? false : {duration: 250},
  };

  new Chart(document.getElementById('monthly-chart'), {
    type: 'line',
    data: {
      labels,
      datasets: series.map(({name, data}, index) => ({
        label: name,
        data,
        borderColor: palette[index],
        backgroundColor: palette[index],
        borderWidth: 2,
        pointRadius: 3,
      })),
    },
    options: {
      ...options,
      plugins: {legend: {position: 'bottom'}},
      scales: {y: {beginAtZero: true, ticks: {precision: 0}}},
    },
  });

  const stockCanvas = document.getElementById('stock-chart');
  if (!stock.some(({total}) => total)) {
    stockCanvas.parentElement.hidden = true;
    return;
  }
  new Chart(stockCanvas, {
    type: 'doughnut',
    data: {
      labels: stock.map(({label}) => label),
      datasets: [{data: stock.map(({total}) => total), backgroundColor: palette}],
    },
    options: {...options, plugins: {legend: {display: false}}},
  });
})();
