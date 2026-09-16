"""Immutable oracle for helios-north:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The catalog path retains 42 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
