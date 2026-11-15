(() => {
  const data = document.getElementById("department-program-data");
  const departmentInput = document.getElementById("department");
  const programInput = document.getElementById("program");
  const programOptions = document.getElementById("program-options");
  if (!data || !departmentInput || !programInput || !programOptions) return;

  const programsByDepartment = JSON.parse(data.textContent || "{}");
  const departmentOptions = Array.from(
    document.getElementById("department-options").options
  );

  function selectedDepartment() {
    const value = departmentInput.value.trim().toLowerCase();
    const match = departmentOptions.find((option) =>
      option.value.toLowerCase() === value
      || (option.label || "").toLowerCase() === value
    );
    return match ? match.value : "";
  }

  function updatePrograms(clearSelection) {
    const department = selectedDepartment();
    const programs = programsByDepartment[department] || [];
    programOptions.replaceChildren();
    programs.forEach((program) => {
      const option = document.createElement("option");
      option.value = program.code;
      option.label = program.name;
      programOptions.appendChild(option);
    });
    programInput.disabled = programs.length === 0;
    programInput.placeholder = programs.length
      ? "Type or select a program"
      : "Select a valid department first";
    if (clearSelection || !programs.some((program) =>
      program.code.toLowerCase() === programInput.value.trim().toLowerCase()
      || program.name.toLowerCase() === programInput.value.trim().toLowerCase()
    )) {
      programInput.value = "";
    }
  }

  departmentInput.addEventListener("input", () => updatePrograms(true));
  departmentInput.addEventListener("change", () => updatePrograms(true));
  updatePrograms(false);
})();
