/* Subject code / description pop-up list (General Comments #10).
 * Typing in either field shows matching subjects; picking one fills both. */
(function () {
  var dataEl = document.getElementById('subject-data');
  if (!dataEl) return;
  var subjects = JSON.parse(dataEl.textContent || '[]');
  var codeInput = document.getElementById('subject_code');
  var nameInput = document.getElementById('subject_name');
  var lists = { code: document.getElementById('suggest-code'), name: document.getElementById('suggest-name') };
  var inputs = { code: codeInput, name: nameInput };
  var active = { code: -1, name: -1 };

  function pick(subject) {
    codeInput.value = subject.code;
    nameInput.value = subject.name;
    hide('code'); hide('name');
  }
  function hide(kind) { lists[kind].hidden = true; active[kind] = -1; }

  function render(kind) {
    var term = inputs[kind].value.trim().toLowerCase();
    var list = lists[kind];
    var matches = subjects.filter(function (s) {
      return !term || s.code.toLowerCase().indexOf(term) > -1 || s.name.toLowerCase().indexOf(term) > -1;
    }).slice(0, 8);
    list.innerHTML = '';
    active[kind] = -1;
    if (!matches.length) { list.hidden = true; return; }
    matches.forEach(function (s) {
      var li = document.createElement('li');
      li.setAttribute('role', 'option');
      var strong = document.createElement('strong');
      strong.textContent = s.code;
      li.appendChild(strong);
      li.appendChild(document.createTextNode(' \u2014 ' + s.name));
      li.addEventListener('mousedown', function (e) { e.preventDefault(); pick(s); });
      list.appendChild(li);
    });
    list.hidden = false;
  }

  ['code', 'name'].forEach(function (kind) {
    var input = inputs[kind];
    input.addEventListener('input', function () {
      render(kind);
      // exact match on one field completes the other automatically
      var v = input.value.trim().toLowerCase();
      var exact = subjects.find(function (s) { return (kind === 'code' ? s.code : s.name).toLowerCase() === v; });
      if (exact) { (kind === 'code' ? nameInput : codeInput).value = kind === 'code' ? exact.name : exact.code; }
    });
    input.addEventListener('focus', function () { render(kind); });
    input.addEventListener('blur', function () { setTimeout(function () { hide(kind); }, 120); });
    input.addEventListener('keydown', function (e) {
      var items = lists[kind].querySelectorAll('li');
      if (lists[kind].hidden || !items.length) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        active[kind] = (active[kind] + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items.forEach(function (li, i) { li.classList.toggle('active', i === active[kind]); });
      } else if (e.key === 'Enter' && active[kind] > -1) {
        e.preventDefault();
        items[active[kind]].dispatchEvent(new MouseEvent('mousedown'));
      } else if (e.key === 'Escape') { hide(kind); }
    });
  });
})();
