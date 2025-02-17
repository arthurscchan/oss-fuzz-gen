"""The Analysis Stage class for examining the performance of fuzz targets. This
stage is responsible for categorizing run-time crashes and detecting untested
code blocks."""
from results import Result
from stage.base_stage import BaseStage


class AnalysisStage(BaseStage):
  """Analyzes the runtime performance of fuzz targets and suggests improvements.
  This stage examines whether crashes are due to bugs in the fuzz target or
  if there are significant code blocks left uncovered. Based on this analysis,
  it provides recommendations for refining the fuzz target in subsequent stages.
  Additionally, it prepares to terminate the experiment if the fuzz target
  crashes due to a bug in the project under test or if all major code paths have
  been sufficiently covered."""

  def execute(self, result_history: list[Result]) -> Result:
    self.logger.info('Analysis Stage')
    agent = self.get_agent()
    analysis_result = agent.execute(result_history)

    # TODO(dongge): Save logs and more info into workdir.
    self.logger.write_chat_history(analysis_result)
    self.logger.debug('Analysis stage completed with with result:\n%s',
                      analysis_result)
    return analysis_result
