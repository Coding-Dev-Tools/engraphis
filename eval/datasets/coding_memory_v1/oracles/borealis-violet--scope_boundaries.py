"""Immutable oracle for borealis-violet:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-borealis-violet', service.scope_owner()


if __name__ == "__main__":
    main()
