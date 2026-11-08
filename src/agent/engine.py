"""Two-stage planner and executor agent loop using LiteLLM/OpenAI format."""

import json
import logging
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field

import litellm
from src.agent.tools import AgentTools, TOOL_DEFINITIONS
from src.sandbox.config import OpenJulesConfig
from src.sandbox.docker_driver import SandboxContext, SandboxManager

logger = logging.getLogger(__name__)


class PlanOutput(BaseModel):
    """Structured plan generated during Step A."""

    rationale: str = Field(description="Problem analysis and approach rationale")
    target_files: List[str] = Field(
        default_factory=list, description="Files intended to be modified or created"
    )
    planned_tests: List[str] = Field(
        default_factory=list, description="Tests to run or create for verification"
    )
    action_steps: List[str] = Field(
        default_factory=list, description="Ordered step-by-step implementation actions"
    )


class AgentResult(BaseModel):
    """Result of agent execution loop."""

    success: bool
    plan: Optional[PlanOutput] = None
    turns_taken: int = 0
    verification_passed: bool = False
    message: str = ""
    error: Optional[str] = None


class AgentEngine:
    """Orchestrates Step A (Planning), Step B (ReAct Loop), and Step C (Verification)."""

    def __init__(
        self,
        sandbox_manager: SandboxManager,
        model_name: str = "gpt-4o",
        max_turns: int = 25,
        llm_client: Optional[Any] = None,
    ):
        self.sandbox_manager = sandbox_manager
        self.model_name = model_name
        self.max_turns = max_turns
        self.llm_client = llm_client or litellm

    def _call_llm(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Any:
        """Invokes LLM with error handling and fallback."""
        kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return self.llm_client.completion(**kwargs)

    def step_a_planning(
        self,
        context: SandboxContext,
        task_instruction: str,
        tools: AgentTools,
    ) -> PlanOutput:
        """Step A: Inspect repository context and generate structured JSON plan."""
        # Probe directory tree and README
        file_tree = tools.list_files(".", max_depth=2)
        readme_preview = tools.read_file("README.md", offset=1, limit=50)

        planning_prompt = (
            f"You are OpenJules, an autonomous software engineering agent.\n"
            f"Task Instruction:\n{task_instruction}\n\n"
            f"Repository file listing:\n{file_tree}\n\n"
            f"README snippet:\n{readme_preview}\n\n"
            f"Test verification command configured: {context.config.test_command}\n\n"
            f"Formulate a structured JSON plan with the following keys:\n"
            f"- rationale: string\n"
            f"- target_files: list of file paths\n"
            f"- planned_tests: list of verification tests\n"
            f"- action_steps: list of steps to execute\n"
            f"Respond ONLY with valid JSON."
        )

        messages = [
            {"role": "system", "content": "You are a senior software architect planning a code change. Always return valid JSON."},
            {"role": "user", "content": planning_prompt},
        ]

        try:
            response = self._call_llm(messages)
            content = response.choices[0].message.content or "{}"
            # Extract json if wrapped in ```json
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            plan_dict = json.loads(content)
            return PlanOutput(**plan_dict)
        except Exception as e:
            logger.warning(f"Failed to parse LLM plan as JSON: {e}. Falling back to default plan.")
            return PlanOutput(
                rationale=f"Plan to execute task: {task_instruction[:100]}",
                target_files=[],
                planned_tests=[context.config.test_command],
                action_steps=["Examine repository", "Make requested changes", "Run tests"],
            )

    def execute_tool(self, tools: AgentTools, tool_name: str, args: Dict[str, Any]) -> str:
        """Dispatches tool call to AgentTools."""
        if tool_name == "read_file":
            return tools.read_file(
                path=args.get("path", ""),
                offset=args.get("offset", 1),
                limit=args.get("limit", 200),
            )
        elif tool_name == "write_file":
            return tools.write_file(
                path=args.get("path", ""),
                content=args.get("content", ""),
            )
        elif tool_name == "apply_patch":
            return tools.apply_patch(
                path=args.get("path", ""),
                patch_str=args.get("patch_str", ""),
            )
        elif tool_name == "run_command":
            return tools.run_command(
                command=args.get("command", ""),
                timeout=args.get("timeout", 120),
            )
        elif tool_name == "list_files":
            return tools.list_files(
                directory=args.get("directory", "."),
                max_depth=args.get("max_depth", 3),
            )
        else:
            return f"Unknown tool: {tool_name}"

    def step_b_action_loop(
        self,
        context: SandboxContext,
        task_instruction: str,
        plan: PlanOutput,
        tools: AgentTools,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Step B: Stateful ReAct loop with max turn limit."""
        system_prompt = (
            "You are OpenJules, an autonomous software engineering agent. "
            "Your goal is to inspect the codebase, edit code, run commands, and verify your changes. "
            "Use the provided tools to inspect files, edit files, and run commands. "
            "Once you have completed all changes and verified them, reply with 'TASK_COMPLETED'."
        )

        plan_summary = (
            f"Approved Plan:\n"
            f"Rationale: {plan.rationale}\n"
            f"Target Files: {plan.target_files}\n"
            f"Steps: {plan.action_steps}\n"
        )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Task: {task_instruction}\n\n{plan_summary}"},
        ]

        turns = 0
        while turns < self.max_turns:
            turns += 1
            if on_progress:
                on_progress(f"Executing turn {turns}/{self.max_turns}...")

            response = self._call_llm(messages, tools=TOOL_DEFINITIONS)
            msg = response.choices[0].message

            # Add assistant message to history
            assistant_msg_dict: Dict[str, Any] = {
                "role": "assistant",
                "content": msg.content,
            }
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                assistant_msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_msg_dict)

            # Check if finished
            if not getattr(msg, "tool_calls", None):
                if msg.content and "TASK_COMPLETED" in msg.content:
                    break
                # If LLM didn't call tools and didn't say task completed, break or nudge
                break

            # Execute tool calls
            for tc in msg.tool_calls:
                func_name = tc.function.name
                raw_args = tc.function.arguments
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    args = {}

                tool_output = self.execute_tool(tools, func_name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": func_name,
                        "content": str(tool_output),
                    }
                )

        return turns

    def step_c_verification(
        self,
        context: SandboxContext,
        tools: AgentTools,
        fix_attempts: int = 3,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Step C: Automatically execute config.test_command and loop fixes if failing."""
        test_cmd = context.config.test_command
        if not test_cmd:
            return True

        for attempt in range(1, fix_attempts + 1):
            if on_progress:
                on_progress(f"Running verification: '{test_cmd}' (attempt {attempt}/{fix_attempts})...")

            res = self.sandbox_manager.exec_in_sandbox(context.container_id, test_cmd)
            if res.exit_code == 0:
                return True

            if attempt == fix_attempts:
                logger.warning(f"Verification failed on final attempt:\n{res.stdout}\n{res.stderr}")
                return False

            # If tests failed, feed failure back to model to fix
            feedback_prompt = (
                f"The test verification command '{test_cmd}' failed with exit code {res.exit_code}.\n"
                f"[STDOUT]:\n{res.stdout}\n"
                f"[STDERR]:\n{res.stderr}\n\n"
                f"Analyze the traceback and fix the code using your tools."
            )
            messages = [
                {"role": "system", "content": "You are OpenJules. Fix the failing tests using your tools."},
                {"role": "user", "content": feedback_prompt},
            ]

            response = self._call_llm(messages, tools=TOOL_DEFINITIONS)
            msg = response.choices[0].message
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except Exception:
                        args = {}
                    self.execute_tool(tools, tc.function.name, args)

        return False

    def run(
        self,
        context: SandboxContext,
        task_instruction: str,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> AgentResult:
        """Executes full Step A -> Step B -> Step C workflow."""
        tools = AgentTools(self.sandbox_manager, context.container_id)

        # Step A: Planning
        if on_progress:
            on_progress("Step A: Formulating plan...")
        plan = self.step_a_planning(context, task_instruction, tools)

        # Step B: ReAct action loop
        if on_progress:
            on_progress("Step B: Executing action loop...")
        turns = self.step_b_action_loop(context, task_instruction, plan, tools, on_progress)

        # Step C: Verification
        if on_progress:
            on_progress("Step C: Verifying test suite...")
        verified = self.step_c_verification(context, tools, on_progress=on_progress)

        return AgentResult(
            success=verified,
            plan=plan,
            turns_taken=turns,
            verification_passed=verified,
            message="Task executed and verified" if verified else "Task executed but verification failed",
        )
