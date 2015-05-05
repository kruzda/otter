"""
Test cron scheduler policies are executed via change, change percent
and desired caapacity
"""
from cafe.drivers.unittest.decorators import tags

from test_repo.autoscale.fixtures import AutoscaleFixture


class CronStyleSchedulerTests(AutoscaleFixture):

    """
    Verify cron style scheduler policy executes for all
    policy change types
    """

    def setUp(self):
        """
        Create a scaling group with minentities=0 and cooldown=0
        """
        super(CronStyleSchedulerTests, self).setUp()
        response = self.autoscale_behaviors.create_scaling_group_given(
            lc_name='cron_style_scheduled',
            gc_cooldown=0)
        self.group = response.entity
        self.resources.add(self.group, self.empty_scaling_group)

    @tags(speed='slow', convergence='yes')
    def test_system_cron_style_change_policy_up_down(self):
        """
        Create a cron style schedule policy via change to scale up by 2,
        followed by a cron style schedule policy to scale down by -2,
        each policy with 0 cooldown. The total servers after execution of both
        policies is the minentities with which the group was created.
        """
        self.autoscale_behaviors.create_schedule_policy_given(
            group_id=self.group.id,
            sp_cooldown=360,
            sp_change=self.sp_change,
            schedule_cron='* * * * *')
        self.wait_for_expected_group_state(
            self.group.id, self.sp_change,
            60 + self.scheduler_interval, 2, time_scale=False)
        self.autoscale_behaviors.create_schedule_policy_given(
            group_id=self.group.id,
            sp_cooldown=360,
            sp_change=-self.sp_change,
            schedule_cron='* * * * *')
        self.wait_for_expected_group_state(
            self.group.id, self.group.groupConfiguration.minEntities,
            60 + self.scheduler_interval, 2, time_scale=False)

    @tags(speed='slow', convergence='yes')
    def test_system_cron_style_desired_capacity_policy_up_down(self):
        """
        Create a cron style schedule policy via desired capacity to scale
        up by 1, followed by a cron style schedule policy to scale down to 0,
        each policy with 0 cooldown. The total servers after execution of both
        policies is 0.
        """
        self.autoscale_behaviors.create_schedule_policy_given(
            group_id=self.group.id,
            sp_cooldown=360,
            sp_desired_capacity=self.sp_desired_capacity,
            schedule_cron='* * * * *')
        self.wait_for_expected_group_state(
            self.group.id, self.sp_desired_capacity,
            60 + self.scheduler_interval, 2, time_scale=False)
        self.autoscale_behaviors.create_schedule_policy_given(
            group_id=self.group.id,
            sp_cooldown=360,
            sp_desired_capacity=self.group.groupConfiguration.minEntities,
            schedule_cron='* * * * *')
        self.wait_for_expected_group_state(
            self.group.id, self.group.groupConfiguration.minEntities,
            60 + self.scheduler_interval, 2, time_scale=False)

    @tags(speed='slow', convergence='yes')
    def test_system_cron_style_policy_cooldown(self):
        """
        Create a cron style scheduler policy via change to scale up
        with cooldown>0, wait for it to execute. Re-execute the policy
        manually before the cooldown results in 403.
        """
        cron_policy = self.autoscale_behaviors.create_schedule_policy_given(
            group_id=self.group.id,
            sp_cooldown=75,
            sp_change=self.sp_change,
            schedule_cron='* * * * *')
        self.wait_for_expected_group_state(
            self.group.id, self.sp_change,
            60 + self.scheduler_interval, 2, time_scale=False)
        execute_scheduled_policy = self.autoscale_client.execute_policy(
            group_id=self.group.id,
            policy_id=cron_policy['id'])
        self.assertEquals(execute_scheduled_policy.status_code, 403)

    @tags(speed='slow', convergence='yes')
    def test_system_cron_style_policy_executes_again(self):
        """
        1-minute Cron-style policy executes in a minute and then again
        after 1 minute
        """
        self.autoscale_behaviors.create_schedule_policy_given(
            group_id=self.group.id,
            sp_cooldown=0,
            sp_change=self.sp_change,
            schedule_cron='* * * * *')
        # Wait for first execution
        self.wait_for_expected_group_state(
            self.group.id,
            self.group.groupConfiguration.minEntities + self.sp_change,
            60 + self.scheduler_interval, 2, time_scale=False)
        # Now wait for next execution
        self.wait_for_expected_group_state(
            self.group.id,
            self.group.groupConfiguration.minEntities + self.sp_change * 2,
            60 + self.scheduler_interval, 2, time_scale=False)
