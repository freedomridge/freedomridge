# On-demand compute workers

A server can start a compute worker itself when a compute job, such as a map re-fit, has waited with no worker free
to take it, and the worker's machine powers off once it has been idle. A deployment without a machine of its own for
heavy jobs pays only for the minutes they run: a spot machine with 64 GB of RAM costs cents an hour.

It's off until an admin turns it on under **Settings → Compute Workers → On-demand workers**. AWS EC2 is the only
provider so far.

## How it works

Once a minute, the job runner:

1. **Ends machines that are done:** AWS ended it (its worker exited when idle, or AWS took a spot machine back); it
   has run longer than the hours a machine may; its worker never connected within 15 minutes of the launch; or its
   worker has been silent for 10 minutes. Each machine's worker token is revoked when it ends.
2. **Starts one machine** when a compute job has waited at least the wait time, no connected worker is free to take
   it, no started machine is still connecting, and the limits allow it. The job says "Starting a compute worker".

A machine is a spot instance of the configured type, launched on-demand only when AWS has no spot capacity for it.
It boots AWS's current Amazon Linux 2023 (read from AWS's public SSM parameter, so there is no image to maintain),
installs Docker, and runs `python -m fridge.worker --exit-when-idle` from this server's version of the `backend`
image. When the worker exits, the machine powers off, and powering off deletes it.

If AWS takes a spot machine back, AWS stops the worker first, which gives its job back: the job goes back in the
queue without counting as a failure, and another machine is started for it.

The machine has no AWS role: it gets its work from this server over the API, with a worker token made for that
machine. The token expires an hour after the machine's time limit and is revoked when the machine ends. It is in
the machine's startup script, which only someone allowed to read the instance's attributes in AWS can see.

## Limits

All are times, set on the settings page:

- **Machines at once** (1): no machine starts while this many are running.
- **Hours per machine** (6): a machine running longer is ended, whatever it's doing; its job goes back in the queue.
- **Machine-hours a month** (40): once the month's machines have run this long, no machine starts until the next
  month. The page shows the hours used.
- **Wait** (5 minutes): how long a job waits for a connected worker before a machine is started for it.
- **Idle exit** (10 minutes): how long a machine's worker waits for more work before it exits.

When a limit holds a job back, the job says which.

## Setting it up

1. **Leave compute jobs to workers:** set `JOB_RUNNER_PLACEMENTS=server` for the job runner, in `.env` or the
   compose file. Otherwise the server's own job runner does the fits and nothing ever waits for a worker.
2. **Network:** the machines need outbound internet access, to pull the image from ghcr.io and to reach the
   server's URL. The region's default VPC gives them that. Set a subnet and security groups to use others; a private
   subnet needs a NAT gateway.
3. **Permissions:** the server's own AWS credentials (an EC2 instance role, say) or an access key saved on the
   settings page need this policy, for the region you use:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "ec2:RunInstances",
     "Resource": ["arn:aws:ec2:REGION:*:instance/*", "arn:aws:ec2:REGION:*:volume/*"],
     "Condition": {"StringLike": {"aws:RequestTag/fridge:launch": "*"}}},
    {"Effect": "Allow", "Action": "ec2:RunInstances",
     "Resource": ["arn:aws:ec2:REGION::image/*", "arn:aws:ec2:REGION:*:subnet/*",
                  "arn:aws:ec2:REGION:*:network-interface/*", "arn:aws:ec2:REGION:*:security-group/*",
                  "arn:aws:ec2:REGION:*:spot-instances-request/*"]},
    {"Effect": "Allow", "Action": "ec2:CreateTags",
     "Resource": ["arn:aws:ec2:REGION:*:instance/*", "arn:aws:ec2:REGION:*:volume/*"],
     "Condition": {"StringEquals": {"ec2:CreateAction": "RunInstances"}}},
    {"Effect": "Allow", "Action": "ec2:TerminateInstances", "Resource": "arn:aws:ec2:REGION:*:instance/*",
     "Condition": {"StringLike": {"aws:ResourceTag/fridge:launch": "*"}}},
    {"Effect": "Allow", "Action": ["ec2:DescribeInstances", "ec2:DescribeInstanceTypes"], "Resource": "*"},
    {"Effect": "Allow", "Action": "ssm:GetParameters",
     "Resource": "arn:aws:ssm:REGION::parameter/aws/service/ami-amazon-linux-latest/*"}
  ]
}
```

   It may start instances only with the `fridge:launch` tag, and terminate only those. The tag names the server's
   database and the launch, so several servers can share one AWS account. The repository's Terraform
   attaches it to the server's role with `on_demand_workers = true`.
4. **Settings:** the region, the instance type, and the URL the machines reach this server's API at.
   - The instance type is Arm (Graviton) or x86_64; the machine's image follows it. The default, `r7g.2xlarge`
     (Graviton, 8 vCPUs, 64 GiB), costs less than the x86_64 `r7i.2xlarge` of the same size.
   - Its memory bounds the biggest fit, which sizes itself to it.

   **Check with AWS** asks AWS whether it would start a machine with the saved settings, as a dry run: nothing
   starts. Then turn it on.

The machines started, their type, state, hours and why each ended, are listed on the same page. A worker started on
demand also shows among the compute workers while it runs, as `on-demand-<n>`.
